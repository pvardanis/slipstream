#!/usr/bin/env bash
# Bring up the client-side mutual-TLS proxy on the ephemeral bench host. The boot
# script installs nginx and drops /etc/nginx/nginx.conf (the rendered stream
# config) and /etc/bench-proxy/proxy.env, but does not start nginx: the client
# certificate lives in Secrets Manager and the ALB targets are not healthy at boot
# yet. The sweep orchestration runs this over SSM just before the sweep. It fetches
# the bench-client secret, writes the cert, key and CA into the proxy dir
# (/etc/bench-proxy, the paths the nginx config references), starts nginx, and
# smokes /health through the proxy so a broken handshake fails here rather than
# mid-sweep. Idempotent: safe to re-run.
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
# The splitter reads the JSON from $SECRET_JSON (off argv, off the process list) and
# writes each PEM field 0600; it fails with a named message when a field or the whole
# SecretString is absent.
python3 /usr/local/bin/bench_proxy_secret_split.py \
  "${client_cert}" "${client_key}" "${ca_cert}"

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
  # Bound each probe: an L4 proxy can accept the loopback TCP connection and then
  # hang on a stalled upstream handshake (unreachable target, black-holed packets),
  # which without a ceiling would freeze the retry loop until the outer SSM command
  # times out minutes later instead of failing here with a clear message.
  status="$(curl -s --connect-timeout 3 --max-time 5 -o /dev/null -w '%{http_code}' \
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
