#!/usr/bin/env bash
# Unit test for scripts/grafana-cloud-setup.sh: terraform, aws and the browser openers are
# stubbed on PATH and the operator's answers are piped on stdin, so no apply, AWS call or
# browser runs. It checks that the external ID lands in terraform.tfvars beside the values
# already there before the apply reads it, and a first run creates the file at mode 0600;
# that the stack URL is written into the grafana stack's terraform.tfvars, without its
# trailing slash and beside the values already there, only once every token is stored,
# replacing a stored one that is not a stack URL after saying so; that the operator is
# told to delete the CloudWatch data source the external ID page creates, so the grafana
# stack can create its own, and to give the service account the Admin role it needs to
# create it, a new account or an existing one; that the stacks:read token is minted in
# the Cloud Portal, whose access policies offer that scope under Add scope; that each
# token reaches its Secrets Manager secret in the bootstrap region on stdin and never
# appears in an argument, stdout, stderr, xtrace or a file the wizard writes; that an
# invalid external ID or an empty token is asked for again; that the data source, access
# policy, service account and Cloud Portal pages open; that the operator email is asked
# for only when neither terraform.tfvars nor TF_VAR_operator_email sets it; that beside a
# stored stack URL a TF_VAR_grafana_external_id reaches the apply without being asked for
# when terraform.tfvars holds none, and a valid stored one is used over it while an
# invalid one is asked for again, saying why; that a re-run
# over a stored stack URL waits for the operator to keep it, then skips it and the stored
# external ID, opening no page, builds the token pages from it, and keeps each stored
# token, naming its stack, unless the operator answers yes
# to replacing it, Enter keeping it, opening no page for a kept one and still checking a
# kept secret holds a value; that a stack URL asked for in the run asks for the external
# ID and every token again, setting a TF_VAR_grafana_external_id aside and saying so;
# that a missing bootstrap or grafana dir, one the wizard cannot write to, or an
# unreadable terraform.tfvars of either stack stops the wizard before any page opens,
# leaving the file intact; and that a missing tool, an unauthenticated aws, AWS CLI
# history being on, closed stdin, a declined apply, an unreadable bootstrap output, a
# failed write, a failed read, a failed or unexpected token check or a secret left empty
# exits non-zero.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/scripts/grafana-cloud-setup.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

write_token="glc_write-token-SECRET-1"
api_token="glsa_api-token-SECRET-2"
stack_read_token="glc_stack-read-token-SECRET-3"

mkdir -p "${tmp}/bin" "${tmp}/bootstrap" "${tmp}/grafana" "${tmp}/empty"
# The wizard runs under this bash by path, so it still starts when RUN_PATH hides every tool.
bash_bin="$(command -v bash)"

# Stub terraform: init succeeds, apply records the grafana_external_id line terraform.tfvars
# holds when it runs, if any, and always a TF_VAR_grafana_external_id= line, empty when
# the variable is unset; `output -raw <name>` answers from a fixed table; TF_EMPTY_OUTPUT
# answers one output empty.
cat >"${tmp}/bin/terraform" <<EOF
#!/usr/bin/env bash
dir="\${1#-chdir=}"
case "\$2" in
  init) exit 0 ;;
  apply)
    grep '^grafana_external_id' "\${dir}/terraform.tfvars" >"${tmp}/applied-with" || true
    echo "TF_VAR_grafana_external_id=\${TF_VAR_grafana_external_id:-}" >>"${tmp}/applied-with" ;;
  output)
    [[ "\$4" == "\${TF_EMPTY_OUTPUT:-}" ]] && exit 0
    case "\$4" in
      region) echo eu-west-1 ;;
      grafana_cloud_write_secret_name) echo grafana-cloud/write ;;
      grafana_cloud_api_secret_name) echo grafana-cloud/api ;;
      grafana_cloud_stack_read_secret_name) echo grafana-cloud/stack-read ;;
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
# describe-secret answers True when the store holds the secret, as the wizard's query
# answers for a secret with an AWSCURRENT version; AWS_FAIL_DESCRIBE fails one secret's
# describe and AWS_ODD_DESCRIBE answers None for one.
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
  "secretsmanager describe-secret")
    [[ "\${id}" == "\${AWS_FAIL_DESCRIBE:-}" ]] && { echo "AccessDeniedException" >&2; exit 254; }
    [[ "\${id}" == "\${AWS_ODD_DESCRIBE:-}" ]] && { echo None; exit 0; }
    if [[ -e "\${store}/\${id}" ]]; then echo True; else echo False; fi ;;
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
# the API token, and the stack-read token. ANSWERS replaces them all, one per line.
# ANSWER_LINES cuts them short, as a closed stdin does.
answers() {
  {
    if [[ -n "${ANSWERS:-}" ]]; then
      printf '%s\n' "${ANSWERS}"
    else
      printf '%s\n' "" "" "https://slipstream.grafana.net/" "bad id!" "ext-123"
      [[ -z "${EMAIL_ANSWERS:-}" ]] || printf '%s\n' "${EMAIL_ANSWERS}"
      printf '%s\n' "${APPLY_ANSWER:-y}" "" "${write_token}" "${api_token}" "${stack_read_token}"
    fi
  } | head -n "${ANSWER_LINES:-99}"
}

# The answers up to the first token stage of a re-run whose stack URL and external ID
# are stored (TFVARS_SET and STORED_URL): start, keep the stored stack, apply.
rerun_answers=$'\n\ny'
stored_url="https://slipstream.grafana.net"
old_token="glc_old-token-SECRET-0"

fail=0
run() {
  rm -rf "${tmp}/store" "${tmp}/applied-with"
  # PRESET_TOKENS names secrets that already hold old_token, PRESET_EMPTY one that holds
  # an empty value.
  local id
  for id in ${PRESET_TOKENS:-}; do
    mkdir -p "${tmp}/store/${id%/*}"
    printf '%s' "${old_token}" >"${tmp}/store/${id}"
  done
  if [[ -n "${PRESET_EMPTY:-}" ]]; then
    mkdir -p "${tmp}/store/${PRESET_EMPTY%/*}"
    : >"${tmp}/store/${PRESET_EMPTY}"
  fi
  : >"${tmp}/aws-calls"
  : >"${tmp}/opened"
  chmod 700 "${tmp}/bootstrap" "${tmp}/grafana"
  rm -f "${tmp}/bootstrap/"* "${tmp}/grafana/"*
  if [[ -z "${NO_TFVARS:-}" ]]; then
    # TFVARS_SET adds the external ID, and STORED_URL the stack URL, a previous run wrote;
    # EXTERNAL_ID_LINE adds a raw external ID line.
    printf 'operator_email = "op@example.com"\n' >"${tmp}/bootstrap/terraform.tfvars"
    [[ -z "${TFVARS_SET:-}" ]] || printf 'grafana_external_id = "ext-old"\n' >>"${tmp}/bootstrap/terraform.tfvars"
    [[ -z "${EXTERNAL_ID_LINE:-}" ]] || printf '%s\n' "${EXTERNAL_ID_LINE}" >>"${tmp}/bootstrap/terraform.tfvars"
    chmod "${TFVARS_MODE:-600}" "${tmp}/bootstrap/terraform.tfvars"
    printf 'kept = "yes"\n' >"${tmp}/grafana/terraform.tfvars"
    [[ -z "${STORED_URL:-}" ]] || printf 'grafana_url = "%s"\n' "${STORED_URL}" >>"${tmp}/grafana/terraform.tfvars"
    chmod "${GRAFANA_TFVARS_MODE:-600}" "${tmp}/grafana/terraform.tfvars"
  fi
  # BOOTSTRAP_DIR_MODE and GRAFANA_DIR_MODE set the stack dirs' modes; the next run resets them.
  chmod "${BOOTSTRAP_DIR_MODE:-700}" "${tmp}/bootstrap"
  chmod "${GRAFANA_DIR_MODE:-700}" "${tmp}/grafana"
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
    if grep -rqF -e "${write_token}" -e "${api_token}" -e "${stack_read_token}" "${file}"; then
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
if [[ "$(grep -c '^grafana_external_id' "${tfvars}")" == 1 ]]; then ok "external id written once"; else bad "external id written once"; fi
check_file "apply read the new external id" "${tmp}/applied-with" 'grafana_external_id = "ext-123"'
if [[ "$(cat "${tmp}/store/grafana-cloud/write")" == "${write_token}" ]]; then ok "write token stored"; else bad "write token stored"; fi
if [[ "$(cat "${tmp}/store/grafana-cloud/api")" == "${api_token}" ]]; then ok "api token stored"; else bad "api token stored"; fi
if [[ "$(cat "${tmp}/store/grafana-cloud/stack-read")" == "${stack_read_token}" ]]; then ok "stack-read token stored"; else bad "stack-read token stored"; fi
check_file "write token put in region" "${tmp}/aws-calls" "secretsmanager put-secret-value --region eu-west-1 --secret-id grafana-cloud/write --secret-string file:///dev/stdin"
check_file "api token put in region" "${tmp}/aws-calls" "secretsmanager put-secret-value --region eu-west-1 --secret-id grafana-cloud/api --secret-string file:///dev/stdin"
check_file "stack-read token put in region" "${tmp}/aws-calls" "secretsmanager put-secret-value --region eu-west-1 --secret-id grafana-cloud/stack-read --secret-string file:///dev/stdin"
check_file "data source page opened" "${tmp}/opened" "https://slipstream.grafana.net/connections/datasources/new"
check_file "access policies page opened" "${tmp}/opened" "https://slipstream.grafana.net/a/grafana-auth-app"
check_file "service accounts page opened" "${tmp}/opened" "https://slipstream.grafana.net/org/serviceaccounts"
check_file "cloud portal page opened" "${tmp}/opened" "https://grafana.com/profile/org"
if grep -qF "stacks:read" "${tmp}/stdout"; then ok "stack-read token scoped stacks:read"; else bad "stack-read token scoped stacks:read"; fi
check_file "stacks scope found under Add scope" "${tmp}/stdout" \
  "  • Open the Add scope drop-down at the bottom of the dialog and pick stacks; it is not in the scope list by default."
grafana_tfvars="${tmp}/grafana/terraform.tfvars"
check_file "stack url written for the grafana stack" "${grafana_tfvars}" 'grafana_url = "https://slipstream.grafana.net"'
check_file "other grafana tfvars kept" "${grafana_tfvars}" 'kept = "yes"'
if [[ "$(grep -c '^grafana_url' "${grafana_tfvars}")" == 1 ]]; then ok "stack url written once"; else bad "stack url written once"; fi
if grep -qF "Save & test" "${tmp}/stdout"; then bad "cloudwatch data source not saved by hand"; else ok "cloudwatch data source not saved by hand"; fi
if grep -qF "click Delete" "${tmp}/stdout"; then ok "cloudwatch data source the page creates is deleted"; else bad "cloudwatch data source the page creates is deleted"; fi
if grep -qF "role Admin" "${tmp}/stdout"; then ok "service account given the Admin role"; else bad "service account given the Admin role"; fi
if grep -qF "already exists, change its role to Admin" "${tmp}/stdout"; then ok "existing service account moved to Admin"; else bad "existing service account moved to Admin"; fi
check_no_token "tokens never echoed, passed as arguments or written to a file"
if grep -qF "already holds a token" "${tmp}/stdout"; then bad "empty secrets not offered to keep"; else ok "empty secrets not offered to keep"; fi
if [[ "$(ls "${tmp}/grafana")" == terraform.tfvars ]]; then ok "no temp file left beside the grafana tfvars"; else bad "no temp file left beside the grafana tfvars"; fi
if [[ "$(ls "${tmp}/bootstrap")" == terraform.tfvars ]]; then ok "no temp file left beside tfvars"; else bad "no temp file left beside tfvars"; fi
if grep -qF "not a valid external ID" "${tmp}/stdout" && grep -qF "token is empty" "${tmp}/stdout"; then
  ok "invalid external id and empty token asked again"
else
  bad "invalid external id and empty token asked again"
fi

# A re-run with every token already stored keeps each one the operator declines to
# replace: no token is asked for or written, and no token page opens.
all_secrets="grafana-cloud/write grafana-cloud/api grafana-cloud/stack-read"
ANSWERS="${rerun_answers}"$'\nn\nn\nn' TFVARS_SET=1 STORED_URL="${stored_url}" PRESET_TOKENS="${all_secrets}" run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
if grep -qF "put-secret-value" "${tmp}/aws-calls"; then bad "kept tokens not written"; else ok "kept tokens not written"; fi
for id in ${all_secrets}; do
  if [[ "$(cat "${tmp}/store/${id}")" == "${old_token}" ]]; then ok "${id} kept"; else bad "${id} kept"; fi
  if grep -qF "${id} already holds a token" "${tmp}/stdout"; then ok "${id} reported set"; else bad "${id} reported set"; fi
  check_file "${id} checked by its AWSCURRENT version" "${tmp}/aws-calls" \
    "secretsmanager describe-secret --region eu-west-1 --secret-id ${id} --query contains(values(VersionIdsToStages || \`{}\`)[], 'AWSCURRENT') --output text"
done
if grep -qE "grafana-auth-app|serviceaccounts|profile/org" "${tmp}/opened"; then bad "kept tokens open no page"; else ok "kept tokens open no page"; fi
check_file "kept tokens still checked for a value" "${tmp}/stdout" "  ✓ grafana-cloud/stack-read holds a value"

# Answering yes replaces a stored token; a secret with no token is asked for as on a
# first run.
ANSWERS="${rerun_answers}"$'\ny\n'"${write_token}"$'\nn\n'"${stack_read_token}" \
  TFVARS_SET=1 STORED_URL="${stored_url}" PRESET_TOKENS="grafana-cloud/write grafana-cloud/api" run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
if [[ "$(cat "${tmp}/store/grafana-cloud/write")" == "${write_token}" ]]; then ok "replaced token stored"; else bad "replaced token stored"; fi
if [[ "$(cat "${tmp}/store/grafana-cloud/api")" == "${old_token}" ]]; then ok "declined token kept beside a replaced one"; else bad "declined token kept beside a replaced one"; fi
if [[ "$(cat "${tmp}/store/grafana-cloud/stack-read")" == "${stack_read_token}" ]]; then ok "missing token asked for on a re-run"; else bad "missing token asked for on a re-run"; fi
if grep -qF "grafana-cloud/stack-read already holds a token" "${tmp}/stdout"; then bad "missing token not reported set"; else ok "missing token not reported set"; fi
check_no_token "re-run tokens never echoed, passed as arguments or written to a file"

# A re-run whose stack URL and external ID a previous run wrote skips both stages: no
# page opens, nothing about them is asked, the apply reads the stored external ID and
# the stored URL stays as it was.
ANSWERS="${rerun_answers}"$'\nn\nn\nn' TFVARS_SET=1 STORED_URL="https://old.grafana.net" PRESET_TOKENS="${all_secrets}" run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
if [[ ! -s "${tmp}/opened" ]]; then ok "stored stack url and external id open no page"; else bad "stored stack url and external id open no page"; fi
if grep -qF "grafana_url is already set to https://old.grafana.net" "${tmp}/stdout"; then ok "stored stack url reported"; else bad "stored stack url reported"; fi
if grep -qF "grafana_external_id is already set" "${tmp}/stdout"; then ok "stored external id reported"; else bad "stored external id reported"; fi
check_file "apply read the stored external id" "${tmp}/applied-with" 'grafana_external_id = "ext-old"'
if [[ "$(grep -c '^grafana_url' "${tmp}/grafana/terraform.tfvars")" == 1 ]]; then ok "stored stack url written once"; else bad "stored stack url written once"; fi
check_file "stored stack url kept" "${tmp}/grafana/terraform.tfvars" 'grafana_url = "https://old.grafana.net"'
if grep -qF "Press Enter to keep this stack, or Ctrl-C to stop." "${tmp}/stdout"; then ok "stored stack url waits for the operator"; else bad "stored stack url waits for the operator"; fi
if grep -qF "grafana-cloud/write already holds a token for https://old.grafana.net." "${tmp}/stdout"; then ok "kept token names its stack"; else bad "kept token names its stack"; fi

# Enter at each replace prompt keeps the stored token.
ANSWERS="${rerun_answers}"$'\n\n\n' TFVARS_SET=1 STORED_URL="${stored_url}" PRESET_TOKENS="${all_secrets}" run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
if grep -qF "put-secret-value" "${tmp}/aws-calls"; then bad "Enter keeps a stored token"; else ok "Enter keeps a stored token"; fi

# A stored stack URL with a trailing slash builds the token pages without it and is
# written back without it.
ANSWERS="${rerun_answers}"$'\n'"${write_token}"$'\nn\nn' TFVARS_SET=1 STORED_URL="https://old.grafana.net/" \
  PRESET_TOKENS="grafana-cloud/api grafana-cloud/stack-read" run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
check_file "stored stack url builds the token page" "${tmp}/opened" "https://old.grafana.net/a/grafana-auth-app"
check_file "stored stack url written without its slash" "${tmp}/grafana/terraform.tfvars" 'grafana_url = "https://old.grafana.net"'

# A stack URL asked for in this run may name another stack, so the stored external ID
# and every stored token are asked for again and replaced.
TFVARS_SET=1 PRESET_TOKENS="${all_secrets}" run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
check_file "asked stack url asks the external id again" "${tmp}/bootstrap/terraform.tfvars" 'grafana_external_id = "ext-123"'
if [[ "$(grep -c '^grafana_external_id' "${tmp}/bootstrap/terraform.tfvars")" == 1 ]]; then ok "external id replaced, not duplicated"; else bad "external id replaced, not duplicated"; fi
if grep -qE "already holds a token|grafana_external_id is already set|grafana_url is already set" "${tmp}/stdout"; then bad "asked stack url keeps nothing stored"; else ok "asked stack url keeps nothing stored"; fi
if [[ "$(cat "${tmp}/store/grafana-cloud/write")" == "${write_token}" && "$(cat "${tmp}/store/grafana-cloud/api")" == "${api_token}" && "$(cat "${tmp}/store/grafana-cloud/stack-read")" == "${stack_read_token}" ]]; then
  ok "asked stack url replaces every stored token"
else
  bad "asked stack url replaces every stored token"
fi

# A stored stack URL that is not a stack URL is asked for again, saying why, and replaced.
STORED_URL="http://not-a-stack" run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
if grep -qF "⚠ grafana_url in ${tmp}/grafana/terraform.tfvars is not a stack URL" "${tmp}/stdout"; then ok "invalid stored stack url reported"; else bad "invalid stored stack url reported"; fi
if [[ "$(grep -c '^grafana_url' "${tmp}/grafana/terraform.tfvars")" == 1 ]]; then ok "invalid stored stack url replaced, not duplicated"; else bad "invalid stored stack url replaced, not duplicated"; fi
check_file "invalid stored stack url asked for again" "${tmp}/grafana/terraform.tfvars" 'grafana_url = "https://slipstream.grafana.net"'

# A stored stack URL with no stored external ID asks for the external ID.
ANSWERS=$'\n\next-123\ny\nn\nn\nn' STORED_URL="${stored_url}" PRESET_TOKENS="${all_secrets}" run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
check_file "stored stack url with no external id opens the data source page" "${tmp}/opened" "${stored_url}/connections/datasources/new"
check_file "stored stack url with no external id writes the one asked for" "${tmp}/applied-with" 'grafana_external_id = "ext-123"'

# A stored external ID that is not a valid one is asked for again, saying why, and
# replaced, even with TF_VAR_grafana_external_id set, since the line would outrank it.
ANSWERS=$'\n\next-123\ny\nn\nn\nn' EXTERNAL_ID_LINE='grafana_external_id = ""' STORED_URL="${stored_url}" \
  PRESET_TOKENS="${all_secrets}" TF_VAR_grafana_external_id=ext-env run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
if grep -qF "⚠ grafana_external_id in ${tmp}/bootstrap/terraform.tfvars is not a valid external ID" "${tmp}/stdout"; then
  ok "invalid stored external id reported"
else
  bad "invalid stored external id reported"
fi
check_file "invalid stored external id replaced" "${tmp}/applied-with" 'grafana_external_id = "ext-123"'
if [[ "$(grep -c '^grafana_external_id' "${tmp}/bootstrap/terraform.tfvars")" == 1 ]]; then ok "invalid stored external id not duplicated"; else bad "invalid stored external id not duplicated"; fi

# Beside a stored stack URL, an external ID exported as TF_VAR_grafana_external_id with
# none in terraform.tfvars is used as is, not asked for.
ANSWERS="${rerun_answers}"$'\nn\nn\nn' STORED_URL="${stored_url}" PRESET_TOKENS="${all_secrets}" \
  TF_VAR_grafana_external_id=ext-env run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
if grep -q '^grafana_external_id' "${tmp}/bootstrap/terraform.tfvars"; then bad "TF_VAR external id not asked for"; else ok "TF_VAR external id not asked for"; fi
if grep -qF "connections/datasources/new" "${tmp}/opened"; then bad "TF_VAR external id opens no data source page"; else ok "TF_VAR external id opens no data source page"; fi
check_file "apply ran with the TF_VAR external id" "${tmp}/applied-with" "TF_VAR_grafana_external_id=ext-env"
if grep -qF "unset TF_VAR_grafana_external_id to set it again" "${tmp}/stdout"; then ok "TF_VAR external id says how to set it again"; else bad "TF_VAR external id says how to set it again"; fi

# terraform.tfvars takes precedence over TF_VAR_grafana_external_id at apply, so beside a
# stored stack URL a stored external ID is the one reported and used.
ANSWERS="${rerun_answers}"$'\nn\nn\nn' TFVARS_SET=1 STORED_URL="${stored_url}" PRESET_TOKENS="${all_secrets}" \
  TF_VAR_grafana_external_id=ext-env run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
if grep -qF "grafana_external_id is already set" "${tmp}/stdout" && ! grep -qF "comes from TF_VAR_grafana_external_id" "${tmp}/stdout"; then
  ok "stored external id reported over TF_VAR"
else
  bad "stored external id reported over TF_VAR"
fi
check_file "apply read the stored external id over TF_VAR" "${tmp}/applied-with" 'grafana_external_id = "ext-old"'

# An external ID belongs to its stack, so beside a stack URL asked for in the run a
# TF_VAR_grafana_external_id is set aside, saying so, and the one asked for is written.
TF_VAR_grafana_external_id=ext-env run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
check_file "asked stack url asks the external id over TF_VAR" "${tmp}/bootstrap/terraform.tfvars" 'grafana_external_id = "ext-123"'
if grep -qF "⚠ TF_VAR_grafana_external_id is set aside" "${tmp}/stdout"; then ok "TF_VAR set aside says so"; else bad "TF_VAR set aside says so"; fi

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
if grep -q '^grafana_url' "${tmp}/grafana/terraform.tfvars"; then bad "failed write writes no stack url"; else ok "failed write writes no stack url"; fi
AWS_EMPTY_GET=grafana-cloud/write check_aborted "secret left empty" "grafana-cloud/write holds no value"
AWS_FAIL_GET=grafana-cloud/api check_aborted "failed read" "cannot read grafana-cloud/api"
# A failed or unexpected token check stops the wizard even with every token at hand to
# enter, so it is never taken for a secret with no token.
token_answers="${rerun_answers}"$'\n'"${write_token}"$'\n'"${api_token}"$'\n'"${stack_read_token}"
ANSWERS="${token_answers}" TFVARS_SET=1 STORED_URL="${stored_url}" AWS_FAIL_DESCRIBE=grafana-cloud/write \
  check_aborted "failed token check" "cannot check grafana-cloud/write"
ANSWERS="${token_answers}" TFVARS_SET=1 STORED_URL="${stored_url}" AWS_ODD_DESCRIBE=grafana-cloud/write \
  check_aborted "unexpected token check answer" "unexpected answer 'None' checking grafana-cloud/write"
ANSWERS="${rerun_answers}"$'\nn\nn\nn' TFVARS_SET=1 STORED_URL="${stored_url}" PRESET_TOKENS="grafana-cloud/write grafana-cloud/stack-read" \
  PRESET_EMPTY=grafana-cloud/api check_aborted "kept empty secret" "grafana-cloud/api holds no value; rerun the wizard and enter its token, answering y if asked to replace it"
AWS_EMPTY_GET=grafana-cloud/stack-read check_aborted "stack-read secret left empty" "grafana-cloud/stack-read holds no value"
if grep -q '^grafana_url' "${tmp}/grafana/terraform.tfvars"; then bad "empty stack-read secret writes no stack url"; else ok "empty stack-read secret writes no stack url"; fi

# A tfvars file the wizard cannot read stops it before the walkthrough, and the values
# already in it are not lost. Mode 0200 blocks reads only for a non-root user.
TFVARS_MODE=200 check_aborted "unreadable tfvars" "cannot read ${tfvars}"
if [[ ! -s "${tmp}/opened" ]]; then ok "unreadable tfvars stops before any page opens"; else bad "unreadable tfvars stops before any page opens"; fi
chmod 600 "${tfvars}"
check_file "unreadable tfvars left intact" "${tfvars}" 'operator_email = "op@example.com"'
# An unreadable grafana tfvars stops the wizard before the walkthrough, not after it.
GRAFANA_TFVARS_MODE=200 check_aborted "unreadable grafana tfvars" "cannot read ${tmp}/grafana/terraform.tfvars"
if [[ ! -s "${tmp}/opened" ]]; then ok "unreadable grafana tfvars stops before any page opens"; else bad "unreadable grafana tfvars stops before any page opens"; fi
chmod 600 "${tmp}/grafana/terraform.tfvars"
# A stack dir the wizard cannot write to stops it before the walkthrough, not at the
# final write, after every token is stored. Mode 0500 blocks writes only for a non-root user.
BOOTSTRAP_DIR_MODE=500 check_aborted "unwritable bootstrap dir" "cannot write to ${tmp}/bootstrap"
if [[ ! -s "${tmp}/opened" ]]; then ok "unwritable bootstrap dir stops before any page opens"; else bad "unwritable bootstrap dir stops before any page opens"; fi
GRAFANA_DIR_MODE=500 check_aborted "unwritable grafana dir" "cannot write to ${tmp}/grafana"
if [[ ! -s "${tmp}/opened" ]]; then ok "unwritable grafana dir stops before any page opens"; else bad "unwritable grafana dir stops before any page opens"; fi
chmod 700 "${tmp}/bootstrap" "${tmp}/grafana"

exit "${fail}"
