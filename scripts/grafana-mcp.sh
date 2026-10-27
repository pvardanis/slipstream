#!/usr/bin/env bash
# Start the Grafana MCP server (grafana/mcp-grafana) over stdio against the operator's
# Grafana Cloud stack, for Claude Code's project .mcp.json. The server runs read-only
# (--disable-write), as the stack's configuration belongs to the grafana Terraform stack;
# the token is that stack's Admin service account token, so read-only rests on the flag.
# The token is read from the bootstrap stack's Secrets Manager secret at start, so it is
# never written to a file. Stdout and stdin are the MCP protocol's channel, so this script
# writes only to stderr and the commands before the server read no stdin.
# Usage: grafana-mcp.sh <bootstrap-dir> <grafana-dir>
set -euo pipefail
# The token passes through a shell variable, so tracing stays off even under bash -x or an
# inherited SHELLOPTS=xtrace.
set +x

bootstrap_dir="$1"
grafana_dir="$2"

# read_output NAME prints the bootstrap output NAME, failing on a failed read or the
# exit-0-empty-stdout case `-raw` gives for an output that resolves to nothing.
read_output() {
  local value
  if ! value="$(terraform -chdir="${bootstrap_dir}" output -raw "$1" </dev/null)" || [[ -z "${value}" ]]; then
    echo "cannot read bootstrap output $1; check your AWS credentials, or apply the bootstrap stack (\`just bootstrap\`), and restart the server" >&2
    return 1
  fi
  printf '%s' "${value}"
}

# read_tfvars_url FILE prints the grafana_url FILE sets, or nothing when it sets none. A
# grafana_url that is not a quoted string, or one set twice, fails, as Terraform rejects
# a repeated attribute and the value would otherwise fall through to another source.
read_tfvars_url() {
  local lines value
  lines="$(grep -E '^[[:space:]]*grafana_url[[:space:]]*=' "$1" || true)"
  [[ -n "${lines}" ]] || return 0
  if [[ "$(wc -l <<<"${lines}")" -gt 1 ]]; then
    echo "grafana_url is set more than once in $1; keep one" >&2
    return 1
  fi
  value="$(sed -nE 's/^[[:space:]]*grafana_url[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' <<<"${lines}")"
  if [[ -z "${value}" ]]; then
    echo "cannot read grafana_url from $1; set it as grafana_url = \"https://<stack>.grafana.net\", or set GRAFANA_URL" >&2
    return 1
  fi
  printf '%s' "${value}"
}

# The stack URL: GRAFANA_URL, the launcher's own override, first; then as the grafana
# stack's apply takes it, its terraform.tfvars (the file `just grafana-cloud-setup`
# writes) over TF_VAR_grafana_url. A checkout with none, such as a fresh worktree, reads
# the URL the last `just grafana-up` published to the stack's state; terraform init writes
# to stderr there, as stdout is the protocol's.
tfvars="${grafana_dir}/terraform.tfvars"
url="" source=""
if [[ -n "${GRAFANA_URL:-}" ]]; then
  url="${GRAFANA_URL}" source=GRAFANA_URL
else
  if [[ -f "${tfvars}" ]]; then
    url="$(read_tfvars_url "${tfvars}")" source="${tfvars}"
  fi
  if [[ -z "${url}" && -n "${TF_VAR_grafana_url:-}" ]]; then
    url="${TF_VAR_grafana_url}" source=TF_VAR_grafana_url
  fi
fi
if [[ -z "${url}" ]]; then
  bucket="$(read_output state_bucket_name)"
  if ! terraform -chdir="${grafana_dir}" init -input=false -backend-config="bucket=${bucket}" </dev/null >&2; then
    echo "cannot initialise the grafana stack against the state bucket ${bucket} (terraform's error above); fix that, or set GRAFANA_URL" >&2
    exit 1
  fi
  if ! url="$(terraform -chdir="${grafana_dir}" output -raw grafana_url </dev/null)" || [[ -z "${url}" ]]; then
    echo "cannot read the grafana stack's grafana_url output; run \`just grafana-up\` to publish it, or set GRAFANA_URL" >&2
    exit 1
  fi
  source="the grafana stack's state"
fi
echo "grafana-mcp: Grafana stack ${url}, from ${source}" >&2

region="$(read_output region)"
secret="$(read_output grafana_cloud_api_secret_name)"

# The aws error stays on stderr, so a credentials or access failure reads as itself. A
# secret with no SecretString prints `None`.
if ! token="$(aws secretsmanager get-secret-value --region "${region}" --secret-id "${secret}" \
  --query SecretString --output text </dev/null)" || [[ -z "${token}" || "${token}" == None ]]; then
  echo "cannot read the Grafana Cloud API token from ${secret}; check your AWS credentials, or run \`just grafana-cloud-setup\` to fill the secret" >&2
  exit 1
fi

export GRAFANA_URL="${url}" GRAFANA_SERVICE_ACCOUNT_TOKEN="${token}"
exec uvx mcp-grafana==2.1.1 --disable-write --usage-stats=disabled
