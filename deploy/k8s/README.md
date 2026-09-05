# Deploying the interaction layer to k3s

One workload: the page where the facts no sensor produces get recorded (ADR-006
corollary (g)). Published at <https://health.int.dev2.net>.

**Ingest is not here and cannot be.** `ah-sync` reads the iCloud Drive folder
HealthSync writes to, which lives on the Mac and which no pod can see (ADR-004).
The cluster is strictly downstream of Postgres; nothing in it can make the data
fresher.

Not ArgoCD-managed — neither is tvledger. `apple-health.yaml` is the record of
what should be running; apply it directly.

## A schema change is a deploy, and it goes first

`Store()` migrates on open, and the pod refuses a database newer than the code
it was built from (`database is at schema N but this build knows only M`). That
guard is right — but it means **any local command that opens a Store applies
pending migrations to the database the cluster is using**, and the running pod
fails readiness the moment it does. Not the pod's next restart: immediately, on
the next `/healthz`. `/livez` stays green and nothing restarts, so the only
symptom is a 503.

So when a commit adds a migration, build and roll the image *before* running
anything locally against `APPLE_HEALTH_DSN` — `ah-query`, `ah-write`, `ah-sync`
with the DSN set, or a one-off `python -c` that constructs a `Store`. This has
taken the site down twice, both times from a laptop command that looked
read-only.

Recovering is forward only: build, push, roll. Do **not** delete the
`schema_version` row to let the old pod start — the migration runs again on the
next deploy, and a data migration that is not idempotent will corrupt what it
already fixed.

## Build and push

The registry speaks plain HTTP, which `docker push` refuses unless the daemon is
configured to trust it. Rather than change your Docker settings,
`scripts/push_image.py` uploads the OCI layout that `docker save` already
produces:

```bash
docker build --platform linux/arm64 -t registry.int.dev2.net:5000/apple-health:latest .
docker save registry.int.dev2.net:5000/apple-health:latest -o tmp/apple-health.tar
uv run --with httpx python scripts/push_image.py tmp/apple-health.tar 172.16.22.24:30500 apple-health latest
```

Push via the NodePort (`172.16.22.24:30500`); the cluster pulls via
`registry.int.dev2.net:5000`. Same registry, two addresses — the DNS name
resolves inside the network, the NodePort is what a laptop can reach.

## Secrets

`sealed-db.yaml` and `sealed-oauth2.yaml` are encrypted for this cluster's
sealed-secrets controller and are safe to commit. Regenerate both with
`./seal-secrets.sh`, which reads every value from a file so no plaintext
reaches argv or the shell history.

`dev2-ca.yaml` is a ConfigMap, not a Secret: a CA certificate is public.

## Apply

```bash
kubectl apply -f deploy/k8s/dev2-ca.yaml \
              -f deploy/k8s/sealed-db.yaml \
              -f deploy/k8s/sealed-oauth2.yaml
kubectl apply -f deploy/k8s/apple-health.yaml
```

## Publish

Already done; recorded here so it can be redone.

```bash
cd /Volumes/nicolas-data/Repositories/kube-deployments/tools/site-provision
uv run provision-site health.int.dev2.net \
  -b ras11:30890 -b ras19:30890 -b ras24:30890 -b ras27:30890
```

All four nodes: nothing pins the pod now that the database is a service, so
HAProxy should round-robin across the cluster.

## Single sign-on

The application has no auth code and needs none. An oauth2-proxy sidecar is the
**only** thing the Service exposes — `targetPort: proxy`, never the app's 8765 —
so every request has been through Authelia before `ah-web` sees it.

Two things make that airtight, and both look like details:

- `ah-web` binds `127.0.0.1`. On `0.0.0.0` the write API, which takes no
  credential, would be reachable by every other pod in the cluster with the
  sidecar simply bypassed.
- The Service's `targetPort` is `proxy`. Pointing it back at the app's own port
  is exactly how you roll SSO back, and exactly how you'd remove it by accident.

The Authelia client lives in `argocd-config`, at
`apps/personal/authelia/configmap.yaml` — the **hash** there, the plaintext only
in the sealed secret. Authelia reads its config once at start-up, so a change
needs `kubectl -n authelia rollout restart deploy/authelia` after ArgoCD syncs.

`authorization_policy: nicolas_only`, not `one_factor`: these pages carry
training history, heart rate and route data, and the built-in policies mean
"any authenticated user" — they would open the site to whoever is added to
Authelia next.

`/healthz` and `/livez` are on `--skip-auth-route` because HAProxy checks the
backend with no browser and no cookie.

## Verifying

HAProxy routes on **SNI, not the Host header**, so pin the name:

```bash
curl -sI --cacert /Volumes/nicolas-data/Repositories/CA/2025/certs/ca.cert.pem \
  https://health.int.dev2.net/          # 302 to auth.int.dev2.net
curl -s http://172.16.22.27:30890/healthz   # coverage instant, no session needed
```

A 302 carrying `client_id=apple-health` is the gate working. If it lands on
`error=invalid_client`, Authelia has not been told about the client yet.

## The database connection: mutual TLS

`APPLE_HEALTH_DSN` uses `sslmode=verify-full` with `sslrootcert`, `sslcert` and
`sslkey`. Both directions are checked: we verify the server (chain *and*
hostname, so it proves which server answered) and the server verifies us.

Never downgrade to `require` or `verify-ca` to make something connect. They
encrypt without proving which server answered, which is how a misrouted
connection becomes a silent one.

Confirm what the server actually sees, rather than trusting that a connection
succeeding means the certificate was used:

```sql
SELECT ssl, version, client_dn FROM pg_stat_ssl WHERE pid = pg_backend_pid();
-- (t, TLSv1.3, /C=JP/ST=Tokyo/L=Kita/O=Dev2/CN=apple_health)
```

### Two holders, one identity

The certificate is held by **both** the pod and the Mac. Postgres matches the CN
against the role name, so any second certificate for this role would have to
carry the same CN and would be the same identity anyway — separate certificates
would buy nothing but a second expiry to track.

That matters when changing `pg_hba`: a rule requiring a client certificate for
`apple_health` applies to the Mac too, and the Mac is what runs ingest. Adding
that rule while the Mac still connected on `sslmode=require` would have stopped
`ah-pgsync` — leaving Postgres quietly behind SQLite, which is the exact failure
this project exists to stop. The Mac's copy lives in `~/.config/apple-health/`
beside the password, and `tools/launchd/net.dev2.healthsync.sync.plist` carries
the full DSN.

### Expiry

**2028-01-09, and it does not auto-renew.** Client certificates have no ACME
path, unlike the web certificates. `ca.py list` shows expiry with a Type column.
When it is reissued, both holders need the new copy: reseal for the pod
(`./seal-secrets.sh`) and re-copy into `~/.config/apple-health/` for the Mac.

### Optionally, require it server-side

Deliberately **not** done. The server accepts the certificate but does not demand
one, which is a deliberate stopping point rather than unfinished work: it keeps
mutual TLS from becoming a single point of failure for a connection that is
already verified and password-authenticated.

If you ever do want it, the rule requires root on ras12. In
`/etc/postgresql/17/main/pg_hba.conf`, **above** the catch-all
`host all all 172.16.0.0/16 scram-sha-256` — the first matching rule wins:

```
hostssl apple_health  apple_health  172.16.0.0/16  scram-sha-256  clientcert=verify-full
```

Then `sudo systemctl reload postgresql@17-main`. The reload does not drop
existing connections, so a connection opened before it proves nothing — verify
with a fresh one.

**Before doing that, check every holder.** The rule applies to anything
connecting as `apple_health`, including the Mac, which is what runs ingest. Both
holders present the certificate today; a third that did not would stop working,
and for `ah-pgsync` that means Postgres silently falling behind SQLite.

Requiring both a certificate and a password is deliberate: a stolen certificate
alone is then not enough, and neither is a stolen password.
