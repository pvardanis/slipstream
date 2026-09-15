#!/usr/bin/env bash
# Bring up the client-side mutual-TLS proxy on the ephemeral bench host. The boot
# script installs nginx and drops /etc/nginx/nginx.conf (the rendered stream
# config) and /etc/bench-proxy/proxy.env, but does not start nginx: the client
# certificate lives in Secrets Manager and the ALB targets are not healthy at boot
# yet. The sweep orchestration runs this over SSM just before the sweep. It fetches
# the bench-client secret, writes the cert, key and CA next to the config, starts
# nginx, and smokes /health through the proxy so a broken handshake fails here
# rather than mid-sweep. Idempotent: safe to re-run.
set -euo pipefail

# Config the boot script rendered from Terraform: the secret ARN, its region, and
# the loopback port the stream server listens on.
# shellcheck source=/dev/null
source /etc/bench-proxy/proxy.env

# Cert paths must match the proxy_ssl_* paths in the rendered nginx config.
cert_dir="/etc/bench-proxy"
client_cert="${cert_dir}/client.crt"
client_key="${cert_dir}/client.key"
ca_cert="${cert_dir}/ca.crt"

echo "proxy-up: fetching bench-client credentials" >&2
# --query SecretString keeps the JSON off argv; it is handed to python via the
# environment, not a command-line argument, so it never shows in the process list.
SECRET_JSON="$(aws secretsmanager get-secret-value \
  --secret-id "${SECRET_ARN}" --region "${REGION}" \
  --query SecretString --output text)"
export SECRET_JSON

echo "proxy-up: writing certificate material" >&2
install -d -m 0700 "${cert_dir}"
# umask so the private key and cert land 0600; python writes them from the one
# secret JSON, splitting the three PEM fields into the files nginx reads.
umask 077
python3 - "${client_cert}" "${client_key}" "${ca_cert}" <<'PY'
import json
import os
import sys

data = json.loads(os.environ["SECRET_JSON"])
targets = (
    (sys.argv[1], "client_cert_pem"),
    (sys.argv[2], "client_key_pem"),
    (sys.argv[3], "ca_cert_pem"),
)
for path, field in targets:
    value = data.get(field)
    if not value:
        raise SystemExit(f"bench-client secret field {field} is missing or empty")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(value)
PY

echo "proxy-up: starting nginx" >&2
# nginx.conf is already the rendered stream config; validate it before (re)start so
# a config error surfaces here with a clear message rather than a failed unit.
nginx -t
systemctl enable --now nginx
systemctl reload nginx

echo "proxy-up: smoking /health through the proxy" >&2
# vLLM leaves /health unauthenticated, so the smoke needs no api-key; it only
# proves the mutual-TLS handshake to the ALB completes and a target is healthy.
# The ALB targets can lag behind boot, so retry rather than fail on the first miss.
status=""
for _ in $(seq 30); do
  status="$(curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:${PROXY_PORT}/health" || true)"
  if [[ "${status}" == "200" ]]; then
    echo "proxy-up: ok (proxy handshake and /health verified)" >&2
    exit 0
  fi
  sleep 2
done

echo "proxy-up: /health smoke failed after retries (last status '${status}')" >&2
echo "proxy-up: check 'journalctl -u nginx' and the ALB target health" >&2
exit 1
