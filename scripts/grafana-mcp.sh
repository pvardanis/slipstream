#!/usr/bin/env bash
# Start the Grafana MCP server (grafana/mcp-grafana) over stdio against the operator's
# Grafana Cloud stack, for Claude Code's project .mcp.json. The server runs read-only, as
# the stack's dashboards and data sources belong to the grafana Terraform stack. Its
# service account token is read from the bootstrap stack's Secrets Manager secret at
# start, so it is never written to a file. Stdout is the MCP protocol's channel, so this
# script writes only to stderr.
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
  if ! value="$(terraform -chdir="${bootstrap_dir}" output -raw "$1")" || [[ -z "${value}" ]]; then
    echo "cannot read bootstrap output $1; check your AWS credentials, or apply the bootstrap stack (\`just bootstrap\`), and restart the server" >&2
    return 1
  fi
  printf '%s' "${value}"
}

# The stack URL, as the grafana stack's own apply would take it: the environment first,
# then the terraform.tfvars `just grafana-cloud-setup` writes. A checkout with neither,
# such as a fresh worktree, reads the URL the last `just grafana-up` published to the
# stack's state; terraform init writes to stderr there, as stdout is the protocol's.
url="${GRAFANA_URL:-${TF_VAR_grafana_url:-}}"
if [[ -z "${url}" && -f "${grafana_dir}/terraform.tfvars" ]]; then
  url="$(sed -nE 's/^grafana_url[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/p' "${grafana_dir}/terraform.tfvars" | tail -n 1)"
fi
if [[ -z "${url}" ]]; then
  bucket="$(read_output state_bucket_name)"
  if ! terraform -chdir="${grafana_dir}" init -input=false -backend-config="bucket=${bucket}" >&2 ||
    ! url="$(terraform -chdir="${grafana_dir}" output -raw grafana_url)" || [[ -z "${url}" ]]; then
    echo "cannot read the grafana stack's grafana_url output; run \`just grafana-up\` to publish it, or set GRAFANA_URL" >&2
    exit 1
  fi
fi

region="$(read_output region)"
secret="$(read_output grafana_cloud_api_secret_name)"

# The aws error stays on stderr, so a credentials or access failure reads as itself.
if ! token="$(aws secretsmanager get-secret-value --region "${region}" --secret-id "${secret}" \
  --query SecretString --output text)" || [[ -z "${token}" ]]; then
  echo "cannot read the Grafana Cloud API token from ${secret}; check your AWS credentials, or run \`just grafana-cloud-setup\` to fill the secret" >&2
  exit 1
fi

export GRAFANA_URL="${url}" GRAFANA_SERVICE_ACCOUNT_TOKEN="${token}"
exec uvx mcp-grafana==2.1.1 --disable-write --usage-stats=disabled
