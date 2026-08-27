#!/bin/sh
# Re-seal the two secrets the interaction layer needs, into deploy/k8s/.
#
# Sealed secrets are encrypted for this cluster's controller and are safe to
# commit; the plaintext never goes near the repo, argv or the shell history —
# every value is read from a file.
#
#   deploy/k8s/sealed-db.yaml      APPLE_HEALTH_DB_PASSWORD  (the Postgres role)
#   deploy/k8s/sealed-oauth2.yaml  the oauth2-proxy client and cookie secrets
#   deploy/k8s/sealed-certs.yaml   the Postgres client certificate and its key
#
# Inputs, none of which are in git:
#   ~/.config/apple-health/db-password     written when the role was provisioned
#   tmp/sso/client-secret.txt              plaintext whose pbkdf2 hash is in
#                                          Authelia's configmap
#   tmp/sso/cookie-secret.txt              32 hex characters — see below
#   CA/2025/issued/apple_health/           the client certificate and key. The
#                                          key and the -withkey.pem bundle must
#                                          never be committed anywhere; the CA
#                                          repo's .gitignore already excludes
#                                          them. Only the sealed form travels.
#
# The cookie secret must be hex, not `openssl rand -base64 32`. oauth2-proxy
# runs the value through base64.RawURLEncoding; standard base64 emits `+` and
# `/`, which are outside that alphabet, so the decode fails, the 44-character
# string is used literally, and the container crash-loops on "cookie_secret
# must be 16, 24, or 32 bytes but is 44". Every hex character is in the
# base64url alphabet, so 32 of them always decode to 24 bytes. 24 is correct;
# do not "fix" it.
set -eu
cd "$(dirname "$0")/../.."

seal() {
  kubeseal --format yaml \
    --controller-namespace kube-system \
    --controller-name sealed-secrets-controller
}

kubectl create secret generic apple-health-db --namespace apple-health \
  --from-file=APPLE_HEALTH_DB_PASSWORD="$HOME/.config/apple-health/db-password" \
  --dry-run=client -o yaml | seal > deploy/k8s/sealed-db.yaml

kubectl create secret generic apple-health-oauth2 --namespace apple-health \
  --from-file=OAUTH2_PROXY_CLIENT_SECRET=tmp/sso/client-secret.txt \
  --from-file=OAUTH2_PROXY_COOKIE_SECRET=tmp/sso/cookie-secret.txt \
  --dry-run=client -o yaml | seal > deploy/k8s/sealed-oauth2.yaml

# The certificate's CN must equal the PostgreSQL role name exactly — Postgres
# matches them directly, so CN=apple_health authenticates as apple_health and
# nothing else. Reissue on any rename.
CERTS=/Volumes/nicolas-data/Repositories/CA/2025/issued/apple_health
kubectl create secret generic apple-health-certs --namespace apple-health \
  --from-file=apple_health.crt="$CERTS/apple_health.crt" \
  --from-file=apple_health.key="$CERTS/apple_health.key" \
  --dry-run=client -o yaml | seal > deploy/k8s/sealed-certs.yaml

# The Claude Code subscription token (`claude setup-token`), for the advisor and
# the chat box. NOT an API key: an sk-ant-api key here would make the CLI bill
# the metered API instead, succeed, and say nothing about it.
kubectl create secret generic apple-health-claude --namespace apple-health \
  --from-file=CLAUDE_CODE_OAUTH_TOKEN="$HOME/.config/apple-health/claude-token" \
  --dry-run=client -o yaml | seal > deploy/k8s/sealed-claude.yaml

echo "sealed -> deploy/k8s/sealed-db.yaml deploy/k8s/sealed-oauth2.yaml deploy/k8s/sealed-certs.yaml deploy/k8s/sealed-claude.yaml"
