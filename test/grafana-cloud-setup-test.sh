#!/usr/bin/env bash
# Unit test for bench/grafana-cloud-setup.sh: terraform, aws and the browser openers are
# stubbed on PATH and the operator's answers are piped on stdin, so no apply, AWS call or
# browser runs. It checks that the external ID lands in terraform.tfvars beside the values
# already there before the apply reads it, and a first run creates the file at mode 0600;
# that the stack URL replaces the one in the grafana stack's terraform.tfvars, without its
# trailing slash and beside the values already there, only once both tokens are stored;
# that the operator is told to delete the CloudWatch data source the external ID page
# creates, so the grafana stack can create its own, and to give the service account the
# Admin role it needs to create it, a new account or an existing one; that each token reaches its Secrets Manager secret in the bootstrap region on stdin and
# never appears in an argument, stdout, stderr, xtrace or a file the wizard writes; that
# an invalid external ID or an empty token is asked for again; that the data source,
# access policy and service account pages open; that the operator email is asked for
# only when neither terraform.tfvars nor TF_VAR_operator_email sets it; that a missing
# bootstrap or grafana dir stops the wizard before any page opens; and that a missing
# tool, an unauthenticated aws, AWS CLI history being on, closed stdin, a declined apply, an
# unreadable bootstrap output, a failed write, a failed read, a secret left empty or an
# unreadable terraform.tfvars exits non-zero, the last leaving the file intact.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/bench/grafana-cloud-setup.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

write_token="glc_write-token-SECRET-1"
api_token="glsa_api-token-SECRET-2"

mkdir -p "${tmp}/bin" "${tmp}/bootstrap" "${tmp}/grafana" "${tmp}/empty"
# The wizard runs under this bash by path, so it still starts when RUN_PATH hides every tool.
bash_bin="$(command -v bash)"

# Stub terraform: init succeeds, apply records the external ID terraform.tfvars holds
# when it runs, and `output -raw <name>` answers from a fixed table; TF_EMPTY_OUTPUT
# answers one output empty.
cat >"${tmp}/bin/terraform" <<EOF
#!/usr/bin/env bash
dir="\${1#-chdir=}"
case "\$2" in
  init) exit 0 ;;
  apply) grep '^grafana_external_id' "\${dir}/terraform.tfvars" >"${tmp}/applied-with" ;;
  output)
    [[ "\$4" == "\${TF_EMPTY_OUTPUT:-}" ]] && exit 0
    case "\$4" in
      region) echo eu-west-1 ;;
      grafana_cloud_write_secret_name) echo grafana-cloud/write ;;
      grafana_cloud_api_secret_name) echo grafana-cloud/api ;;
      *) echo "unexpected output \$4" >&2; exit 1 ;;
    esac ;;
  *) echo "unexpected call: \$*" >&2; exit 2 ;;
esac
EOF
# Stub aws: every call's arguments are logged. put-secret-value stores its stdin under
# the secret ID (file:///dev/stdin is how a token stays off the command line);
# get-secret-value answers the stored value's length. AWS_STS_EXIT fails the identity
# check, AWS_CLI_HISTORY answers the cli_history setting, AWS_FAIL_PUT fails one secret's
# write, AWS_EMPTY_GET reports one secret empty and AWS_FAIL_GET fails one secret's read.
cat >"${tmp}/bin/aws" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"${tmp}/aws-calls"
store="${tmp}/store"
id=""
args=("\$@")
for ((i = 0; i < \${#args[@]}; i++)); do
  [[ "\${args[i]}" == --secret-id ]] && id="\${args[i + 1]}"
done
case "\$1 \$2" in
  "sts get-caller-identity")
    [[ -z "\${AWS_STS_EXIT:-}" ]] || { echo "Error when retrieving token from sso: Token has expired" >&2; exit "\${AWS_STS_EXIT}"; } ;;
  "configure get")
    [[ -n "\${AWS_CLI_HISTORY:-}" ]] || exit 1
    echo "\${AWS_CLI_HISTORY}" ;;
  "secretsmanager put-secret-value")
    [[ " \$* " == *" --secret-string file:///dev/stdin "* ]] || { echo "token not read from stdin" >&2; exit 2; }
    [[ "\${id}" == "\${AWS_FAIL_PUT:-}" ]] && { echo "AccessDeniedException" >&2; exit 254; }
    mkdir -p "\${store}/\$(dirname "\${id}")"
    cat >"\${store}/\${id}"
    echo '{"Name": "'"\${id}"'"}' ;;
  "secretsmanager get-secret-value")
    [[ "\${id}" == "\${AWS_EMPTY_GET:-}" ]] && { echo 0; exit 0; }
    [[ "\${id}" == "\${AWS_FAIL_GET:-}" ]] && { echo "AccessDeniedException" >&2; exit 254; }
    wc -c <"\${store}/\${id}" | tr -d ' ' ;;
  *) echo "unexpected call: \$*" >&2; exit 2 ;;
esac
EOF
# Stub every browser opener the wizard may pick, recording the URL it was handed.
for opener in open xdg-open wslview explorer.exe; do
  cat >"${tmp}/bin/${opener}" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$1" >>"${tmp}/opened"
EOF
done
chmod +x "${tmp}/bin/"*

# The operator's answers, in prompt order: start, stack created, stack URL, an invalid
# external ID then a valid one, the operator email answers when the wizard has to ask
# (EMAIL_ANSWERS, one per line), apply, an empty write token then the token,
# and the API token. ANSWER_LINES cuts them short, as a closed stdin does.
answers() {
  {
    printf '%s\n' "" "" "https://slipstream.grafana.net/" "bad id!" "ext-123"
    [[ -z "${EMAIL_ANSWERS:-}" ]] || printf '%s\n' "${EMAIL_ANSWERS}"
    printf '%s\n' "${APPLY_ANSWER:-y}" "" "${write_token}" "${api_token}"
  } | head -n "${ANSWER_LINES:-99}"
}

fail=0
run() {
  rm -rf "${tmp}/store" "${tmp}/applied-with"
  : >"${tmp}/aws-calls"
  : >"${tmp}/opened"
  rm -f "${tmp}/bootstrap/"* "${tmp}/grafana/"*
  if [[ -z "${NO_TFVARS:-}" ]]; then
    printf 'operator_email = "op@example.com"\ngrafana_external_id = "stale"\n' >"${tmp}/bootstrap/terraform.tfvars"
    chmod "${TFVARS_MODE:-600}" "${tmp}/bootstrap/terraform.tfvars"
    printf 'grafana_url = "https://old.grafana.net"\nkept = "yes"\n' >"${tmp}/grafana/terraform.tfvars"
  fi
  # TRACE runs the wizard under bash -x; RUN_PATH replaces PATH, hiding the stubs.
  answers | PATH="${RUN_PATH:-${tmp}/bin:${PATH}}" "${bash_bin}" ${TRACE:+-x} "${script}" "${BOOTSTRAP_ARG:-${tmp}/bootstrap}" \
    "${GRAFANA_ARG:-${tmp}/grafana}" \
    >"${tmp}/stdout" 2>"${tmp}/stderr"
}
ok() { echo "ok   $1"; }
bad() {
  echo "FAIL $1" >&2
  fail=1
}
check_file() {
  local label="$1" file="$2" want="$3"
  if [[ -f "${file}" ]] && grep -qxF -- "${want}" "${file}"; then ok "${label}"; else bad "${label}: '${want}' not in ${file##*/}"; fi
}
check_no_token() {
  local label="$1" file
  for file in "${tmp}/stdout" "${tmp}/stderr" "${tmp}/aws-calls" "${tmp}/bootstrap" "${tmp}/grafana"; do
    if grep -rqF -e "${write_token}" -e "${api_token}" "${file}"; then
      bad "${label}: a token appears in ${file##*/}"
      return
    fi
  done
  ok "${label}"
}
check_aborted() {
  local label="$1" want_err="$2"
  if run; then
    bad "${label}: script exited 0"
  elif ! grep -qF -- "${want_err}" "${tmp}/stderr"; then
    bad "${label}: stderr lacks '${want_err}'"
  else
    ok "${label}"
  fi
}

# Happy path.
run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
tfvars="${tmp}/bootstrap/terraform.tfvars"
check_file "external id written" "${tfvars}" 'grafana_external_id = "ext-123"'
check_file "other tfvars kept" "${tfvars}" 'operator_email = "op@example.com"'
if [[ "$(grep -c '^grafana_external_id' "${tfvars}")" == 1 ]]; then ok "external id replaced, not duplicated"; else bad "external id replaced, not duplicated"; fi
check_file "apply read the new external id" "${tmp}/applied-with" 'grafana_external_id = "ext-123"'
if [[ "$(cat "${tmp}/store/grafana-cloud/write")" == "${write_token}" ]]; then ok "write token stored"; else bad "write token stored"; fi
if [[ "$(cat "${tmp}/store/grafana-cloud/api")" == "${api_token}" ]]; then ok "api token stored"; else bad "api token stored"; fi
check_file "write token put in region" "${tmp}/aws-calls" "secretsmanager put-secret-value --region eu-west-1 --secret-id grafana-cloud/write --secret-string file:///dev/stdin"
check_file "api token put in region" "${tmp}/aws-calls" "secretsmanager put-secret-value --region eu-west-1 --secret-id grafana-cloud/api --secret-string file:///dev/stdin"
check_file "data source page opened" "${tmp}/opened" "https://slipstream.grafana.net/connections/datasources/new"
check_file "access policies page opened" "${tmp}/opened" "https://slipstream.grafana.net/a/grafana-auth-app"
check_file "service accounts page opened" "${tmp}/opened" "https://slipstream.grafana.net/org/serviceaccounts"
grafana_tfvars="${tmp}/grafana/terraform.tfvars"
check_file "stack url written for the grafana stack" "${grafana_tfvars}" 'grafana_url = "https://slipstream.grafana.net"'
check_file "other grafana tfvars kept" "${grafana_tfvars}" 'kept = "yes"'
if [[ "$(grep -c '^grafana_url' "${grafana_tfvars}")" == 1 ]]; then ok "stack url replaced, not duplicated"; else bad "stack url replaced, not duplicated"; fi
if grep -qF "Save & test" "${tmp}/stdout"; then bad "cloudwatch data source not saved by hand"; else ok "cloudwatch data source not saved by hand"; fi
if grep -qF "click Delete" "${tmp}/stdout"; then ok "cloudwatch data source the page creates is deleted"; else bad "cloudwatch data source the page creates is deleted"; fi
if grep -qF "role Admin" "${tmp}/stdout"; then ok "service account given the Admin role"; else bad "service account given the Admin role"; fi
if grep -qF "already exists, change its role to Admin" "${tmp}/stdout"; then ok "existing service account moved to Admin"; else bad "existing service account moved to Admin"; fi
check_no_token "tokens never echoed, passed as arguments or written to a file"
if [[ "$(ls "${tmp}/grafana")" == terraform.tfvars ]]; then ok "no temp file left beside the grafana tfvars"; else bad "no temp file left beside the grafana tfvars"; fi
if [[ "$(ls "${tmp}/bootstrap")" == terraform.tfvars ]]; then ok "no temp file left beside tfvars"; else bad "no temp file left beside tfvars"; fi
if grep -qF "not a valid external ID" "${tmp}/stdout" && grep -qF "token is empty" "${tmp}/stdout"; then
  ok "invalid external id and empty token asked again"
else
  bad "invalid external id and empty token asked again"
fi

# Under xtrace the wizard turns tracing off, so no token reaches the trace on stderr.
TRACE=1 run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
check_no_token "tokens kept out of xtrace"

if grep -qF "operator_email is already set" "${tmp}/stdout"; then ok "operator email in tfvars not asked for"; else bad "operator email in tfvars not asked for"; fi

# A first run asks for the operator email, again while it is not an address or holds a
# character that would need HCL escaping, and creates terraform.tfvars, owner-only, with
# the external ID and the email; the apply sees both.
EMAIL_ANSWERS=$'not-an-email\nop"x@example.com\nnew@example.com' NO_TFVARS=1 run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
if [[ "$(cat "${tfvars}")" == $'grafana_external_id = "ext-123"\noperator_email = "new@example.com"' ]]; then
  ok "first run creates tfvars with the external id and email"
else
  bad "first run creates tfvars with the external id and email"
fi
if [[ -n "$(find "${tfvars}" -perm 0600)" ]]; then ok "first run tfvars is mode 0600"; else bad "first run tfvars is mode 0600"; fi
if [[ -n "$(find "${tmp}/grafana/terraform.tfvars" -perm 0600)" ]]; then ok "first run grafana tfvars is mode 0600"; else bad "first run grafana tfvars is mode 0600"; fi
if [[ "$(grep -c "not an email address" "${tmp}/stdout")" == 2 ]]; then ok "invalid operator email asked again"; else bad "invalid operator email asked again"; fi
check_file "apply read the new external id with no tfvars before" "${tmp}/applied-with" 'grafana_external_id = "ext-123"'

# An operator email exported as TF_VAR_operator_email is used as is, not asked for.
TF_VAR_operator_email=env@example.com NO_TFVARS=1 run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
if [[ "$(cat "${tfvars}")" == 'grafana_external_id = "ext-123"' ]]; then ok "TF_VAR operator email not asked for"; else bad "TF_VAR operator email not asked for"; fi

# Failures stop the wizard with a message naming what to do.
BOOTSTRAP_ARG="${tmp}/missing" check_aborted "missing bootstrap dir" "bootstrap dir ${tmp}/missing not found"
if [[ ! -s "${tmp}/opened" ]]; then ok "missing bootstrap dir stops before any page opens"; else bad "missing bootstrap dir stops before any page opens"; fi
GRAFANA_ARG="${tmp}/missing" check_aborted "missing grafana dir" "grafana dir ${tmp}/missing not found"
if [[ ! -s "${tmp}/opened" ]]; then ok "missing grafana dir stops before any page opens"; else bad "missing grafana dir stops before any page opens"; fi
RUN_PATH="${tmp}/empty" check_aborted "missing terraform" "terraform not found on PATH"
AWS_STS_EXIT=255 check_aborted "aws not authenticated" "aws is not authenticated"
if grep -qF "Token has expired" "${tmp}/stderr"; then ok "aws's own error shown"; else bad "aws's own error shown"; fi
AWS_CLI_HISTORY=enabled check_aborted "aws cli history on" "cli_history is enabled"
ANSWER_LINES=4 check_aborted "stdin closed at a prompt" "stdin closed before GRAFANA_EXTERNAL_ID was entered"
NO_TFVARS=1 ANSWER_LINES=5 check_aborted "stdin closed at the email" "stdin closed before OPERATOR_EMAIL was entered"
ANSWER_LINES=7 check_aborted "stdin closed at a token" "stdin closed before the token for grafana-cloud/write was entered"
APPLY_ANSWER=n check_aborted "apply declined" "bootstrap not applied"
if [[ ! -e "${tmp}/applied-with" ]]; then ok "declined apply never runs terraform apply"; else bad "declined apply never runs terraform apply"; fi
TF_EMPTY_OUTPUT=region check_aborted "empty bootstrap output" "cannot read bootstrap output region"
AWS_FAIL_PUT=grafana-cloud/api check_aborted "failed write" "writing grafana-cloud/api failed"
check_no_token "failed write echoes no token"
check_file "failed write leaves the stack url as it was" "${tmp}/grafana/terraform.tfvars" 'grafana_url = "https://old.grafana.net"'
AWS_EMPTY_GET=grafana-cloud/write check_aborted "secret left empty" "grafana-cloud/write holds no value"
AWS_FAIL_GET=grafana-cloud/api check_aborted "failed read" "cannot read grafana-cloud/api"

# A tfvars file the wizard can write but not read aborts before the write, so the
# values already in it are not lost. Mode 0200 blocks reads only for a non-root user.
TFVARS_MODE=200 check_aborted "unreadable tfvars" "cannot read ${tfvars}"
chmod 600 "${tfvars}"
check_file "unreadable tfvars left intact" "${tfvars}" 'operator_email = "op@example.com"'

exit "${fail}"
