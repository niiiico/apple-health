# Deploying the interaction layer to k3s

One workload: the page where the facts no sensor produces get recorded (ADR-006
corollary (g)). Published at <https://health.int.dev2.net>.

**Ingest is not here and cannot be.** `ah-sync` reads the iCloud Drive folder
HealthSync writes to, which lives on the Mac and which no pod can see (ADR-004).
The cluster is strictly downstream of Postgres; nothing in it can make the data
fresher.

Not ArgoCD-managed — neither is tvledger. `apple-health.yaml` is the record of
what should be running; apply it directly.

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

## The database connection is not yet at the house standard

`APPLE_HEALTH_DSN` uses `sslmode=require`. That encrypts the connection but
proves nothing about which server answered — `verify-full` with a client
certificate is what tvledger uses and what this should become. Issuing the
certificate needs the Intermediate CA passphrase typed by hand (see the
`add-postgres-user` skill), which is the only reason it is not done.

The remaining work, in order:

1. `uv run /Volumes/nicolas-data/Repositories/CA/2025/ca.py generate apple_health --profile client`
2. Seal `apple_health.crt`, `apple_health.key` and `ca.cert.pem` into
   `apple-health-certs`; mount at `/etc/apple-health/certs` with
   `defaultMode: 0600` — libpq refuses a group- or world-readable key.
3. Switch `APPLE_HEALTH_DSN` to the `verify-full` form spelled out in
   `apple-health.yaml`.
4. Add `hostssl apple_health apple_health 172.16.0.0/16 scram-sha-256
   clientcert=verify-full` to `pg_hba.conf` on ras12 **above** the catch-all —
   first match wins — and `sudo systemctl reload postgresql@17-main`.

Do not resolve a connection failure by downgrading below `require`.
