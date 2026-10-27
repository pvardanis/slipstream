#!/usr/bin/env bash
# Unit test for scripts/grafana-mcp.sh: terraform, aws and uvx are stubbed on PATH so the
# test asserts how the script starts the Grafana MCP server, never a real terraform read,
# AWS call or server. It checks that the server is started read-only at the pinned version
# with the stack URL and the token read from the bootstrap stack's secret, in its region;
# that GRAFANA_URL, then TF_VAR_grafana_url, then the grafana stack's terraform.tfvars
# override the URL published in that stack's state, which is read only when none is set,
# after an init against the bootstrap state bucket; that tfvars is read as Terraform reads
# it (spacing, trailing comments, commented-out lines, the last of repeated lines); that
# the script prints nothing to stdout itself, the MCP protocol's channel, not even
# terraform init's output, and never traces the token; and that a failed init, a failed
# or empty output, or a failed or empty token read aborts before the server starts.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/scripts/grafana-mcp.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# Stub terraform: record `-chdir=<dir> init -input=false -backend-config=bucket=<b>` in
# INIT_LOG, printing to stdout as the real init does, and failing when INIT_EXIT is set;
# answer `-chdir=<dir> output -raw <name>` from a fixed table. EMPTY_OUTPUT names one
# output that resolves to exit-0 empty stdout, FAIL_OUTPUT one whose read fails.
mkdir -p "${tmp}/bin" "${tmp}/bootstrap" "${tmp}/grafana"
cat >"${tmp}/bin/terraform" <<'EOF'
#!/usr/bin/env bash
dir="${1#-chdir=}"
if [[ "$#" == 4 && "$2" == init && "$3" == -input=false && "$4" == -backend-config=bucket=* ]]; then
  echo "Initializing the backend..."
  [[ "${INIT_EXIT:-0}" == 0 ]] || { echo "Error: Failed to get existing workspaces" >&2; exit 1; }
  echo "${dir##*/} ${4#-backend-config=bucket=}" >>"${INIT_LOG}"
  exit 0
fi
[[ "$#" == 4 && "$2" == output && "$3" == -raw ]] || { echo "unexpected call: $*" >&2; exit 2; }
name="$4"
[[ "${name}" == "${EMPTY_OUTPUT:-}" ]] && exit 0
[[ "${name}" == "${FAIL_OUTPUT:-}" ]] && { echo "Output ${name} not found" >&2; exit 1; }
case "${dir##*/}/${name}" in
  bootstrap/region) echo eu-central-1 ;;
  bootstrap/grafana_cloud_api_secret_name) echo stack/api ;;
  bootstrap/state_bucket_name) echo tf-state-bucket ;;
  grafana/grafana_url) echo https://fromstate.grafana.net ;;
  *) echo "unexpected output ${dir}/${name}" >&2; exit 1 ;;
esac
EOF
# Stub aws: print the token for the expected secret read; AWS_EXIT fails the read and
# EMPTY_TOKEN returns an empty secret string.
cat >"${tmp}/bin/aws" <<'EOF'
#!/usr/bin/env bash
want="secretsmanager get-secret-value --region eu-central-1 --secret-id stack/api --query SecretString --output text"
[[ "$*" == "${want}" ]] || { echo "unexpected call: $*" >&2; exit 2; }
[[ "${AWS_EXIT:-0}" == 0 ]] || { echo "AccessDeniedException" >&2; exit 1; }
[[ "${EMPTY_TOKEN:-0}" == 0 ]] && echo glsa_token
exit 0
EOF
# Stub uvx: record the environment the server would see and its arguments.
cat >"${tmp}/bin/uvx" <<EOF
#!/usr/bin/env bash
{
  echo "url=\${GRAFANA_URL:-}"
  echo "token=\${GRAFANA_SERVICE_ACCOUNT_TOKEN:-}"
  echo "args=\$*"
} >"${tmp}/server"
echo server-stdout
EOF
chmod +x "${tmp}/bin/terraform" "${tmp}/bin/aws" "${tmp}/bin/uvx"

tfvars="${tmp}/grafana/terraform.tfvars"
fail=0
run() {
  rm -f "${tmp}/server"
  : >"${tmp}/init-log"
  INIT_LOG="${tmp}/init-log" PATH="${tmp}/bin:${PATH}" "${script}" "${tmp}/bootstrap" "${tmp}/grafana" >"${tmp}/stdout" 2>"${tmp}/stderr"
}
check() {
  local label="$1"
  shift
  if "$@"; then
    echo "ok   ${label}"
  else
    echo "FAIL ${label}" >&2
    fail=1
  fi
}
# The predicates run through check "$@", which shellcheck cannot follow (SC2317 in older
# releases, SC2329 in newer).
# shellcheck disable=SC2317,SC2329
server_has() { grep -qxF -- "$1" "${tmp}/server" 2>/dev/null; }
# shellcheck disable=SC2317,SC2329
stdout_is_the_servers() { [[ "$(cat "${tmp}/stdout")" == server-stdout ]]; }
# check_started LABEL URL [STATE]: the run starts the server with URL, writes nothing to
# stdout but the server's output, and initialises the grafana stack against the
# bootstrap state bucket only when STATE is set.
check_started() {
  local label="$1" want_url="$2" state="${3:-}"
  if ! run; then
    echo "FAIL ${label}: exited non-zero: $(cat "${tmp}/stderr")" >&2
    fail=1
    return
  fi
  check "${label}: URL" server_has "url=${want_url}"
  check "${label}: stdout carries only the server's output" stdout_is_the_servers
  if [[ -n "${state}" ]]; then
    check "${label}: grafana stack initialised against the bootstrap state bucket" grep -qxF "grafana tf-state-bucket" "${tmp}/init-log"
  else
    check "${label}: grafana stack's state not read" test ! -s "${tmp}/init-log"
  fi
}
check_aborted() {
  local label="$1" want_err="$2"
  if run; then
    echo "FAIL ${label}: script exited 0" >&2
    fail=1
  elif [[ -e "${tmp}/server" ]]; then
    echo "FAIL ${label}: server started before abort" >&2
    fail=1
  elif [[ -s "${tmp}/stdout" ]]; then
    echo "FAIL ${label}: wrote to stdout: $(cat "${tmp}/stdout")" >&2
    fail=1
  elif ! grep -qF -- "${want_err}" "${tmp}/stderr"; then
    echo "FAIL ${label}: stderr lacks '${want_err}'" >&2
    fail=1
  else
    echo "ok   ${label}"
  fi
}

printf 'grafana_url = "https://fromtfvars.grafana.net"\n' >"${tfvars}"
check_started "tfvars" https://fromtfvars.grafana.net
check "token read from the bootstrap secret in its region" server_has "token=glsa_token"
check "pinned server version, read-only, no usage stats" server_has "args=mcp-grafana==2.1.1 --disable-write --usage-stats=disabled"
TF_VAR_grafana_url=https://fromtfvar.grafana.net check_started "TF_VAR_grafana_url over tfvars" https://fromtfvar.grafana.net
GRAFANA_URL=https://fromenv.grafana.net TF_VAR_grafana_url=https://fromtfvar.grafana.net \
  check_started "GRAFANA_URL over TF_VAR_grafana_url" https://fromenv.grafana.net
GRAFANA_URL="" TF_VAR_grafana_url=https://fromtfvar.grafana.net check_started "empty GRAFANA_URL falls through" https://fromtfvar.grafana.net

# An inherited SHELLOPTS=xtrace turns tracing on before the script's first line.
env SHELLOPTS=xtrace INIT_LOG="${tmp}/init-log" PATH="${tmp}/bin:${PATH}" "${script}" "${tmp}/bootstrap" "${tmp}/grafana" >/dev/null 2>"${tmp}/stderr" || true
check "xtrace is on for the script's first lines" grep -q 'set -euo pipefail' "${tmp}/stderr"
check "xtrace never traces the token" test "$(grep -c glsa_token "${tmp}/stderr")" = 0

FAIL_OUTPUT=region check_aborted "failed region output aborts" "cannot read bootstrap output region"
EMPTY_OUTPUT=grafana_cloud_api_secret_name check_aborted "empty secret name output aborts" "cannot read bootstrap output grafana_cloud_api_secret_name"
AWS_EXIT=1 check_aborted "failed token read aborts" "just grafana-cloud-setup"
EMPTY_TOKEN=1 check_aborted "empty token aborts" "just grafana-cloud-setup"

# tfvars as Terraform reads it.
printf 'grafana_url="https://nospace.grafana.net" # the stack\n' >"${tfvars}"
check_started "tfvars without spaces, with a trailing comment" https://nospace.grafana.net
printf '# grafana_url = "https://commented.grafana.net"\ngrafana_url = "https://live.grafana.net"\n' >"${tfvars}"
check_started "tfvars commented-out line ignored" https://live.grafana.net
printf 'grafana_url = "https://first.grafana.net"\ngrafana_url = "https://last.grafana.net"\n' >"${tfvars}"
check_started "tfvars repeated line, the last wins" https://last.grafana.net
printf 'region = "eu-west-1"\n' >"${tfvars}"
check_started "tfvars without grafana_url falls through to state" https://fromstate.grafana.net state

# A fresh worktree: no tfvars.
rm "${tfvars}"
GRAFANA_URL=https://fromenv.grafana.net check_started "GRAFANA_URL with no tfvars" https://fromenv.grafana.net
check_started "state with no URL set" https://fromstate.grafana.net state
FAIL_OUTPUT=state_bucket_name check_aborted "failed state bucket output aborts" "cannot read bootstrap output state_bucket_name"
INIT_EXIT=1 check_aborted "failed grafana stack init aborts" "just grafana-up"
FAIL_OUTPUT=grafana_url check_aborted "failed grafana_url output aborts" "just grafana-up"
EMPTY_OUTPUT=grafana_url check_aborted "empty grafana_url output aborts" "just grafana-up"

exit "${fail}"
