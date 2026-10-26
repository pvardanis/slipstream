#!/usr/bin/env bash
# Unit test for bench/grafana-cloud-setup.sh: terraform, aws and the browser openers are
# stubbed on PATH and the operator's answers are piped on stdin, so no apply, AWS call or
# browser runs. It checks that the external ID lands in terraform.tfvars beside the values
# already there before the apply reads it; that each token reaches its Secrets Manager
# secret on stdin and never appears in an argument, stdout or stderr; that an invalid
# external ID or an empty token is asked for again; and that an unauthenticated aws, a
# declined apply, a failed write or a secret left empty exits non-zero.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/bench/grafana-cloud-setup.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

write_token="glc_write-token-SECRET-1"
api_token="glsa_api-token-SECRET-2"

mkdir -p "${tmp}/bin" "${tmp}/bootstrap"

# Stub terraform: init succeeds, apply records the external ID terraform.tfvars holds
# when it runs, and `output -raw <name>` answers from a fixed table.
cat >"${tmp}/bin/terraform" <<EOF
#!/usr/bin/env bash
dir="\${1#-chdir=}"
case "\$2" in
  init) exit 0 ;;
  apply) grep '^grafana_external_id' "\${dir}/terraform.tfvars" >"${tmp}/applied-with" ;;
  output)
    case "\$4" in
      region) echo eu-west-1 ;;
      grafana_cloudwatch_role_arn) echo arn:aws:iam::111122223333:role/slipstream-grafana-cloudwatch ;;
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
# check, AWS_FAIL_PUT fails one secret's write and AWS_EMPTY_GET reports one secret empty.
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
  "sts get-caller-identity") exit "\${AWS_STS_EXIT:-0}" ;;
  "secretsmanager put-secret-value")
    [[ " \$* " == *" --secret-string file:///dev/stdin "* ]] || { echo "token not read from stdin" >&2; exit 2; }
    [[ "\${id}" == "\${AWS_FAIL_PUT:-}" ]] && { echo "AccessDeniedException" >&2; exit 254; }
    mkdir -p "\${store}/\$(dirname "\${id}")"
    cat >"\${store}/\${id}"
    echo '{"Name": "'"\${id}"'"}' ;;
  "secretsmanager get-secret-value")
    [[ "\${id}" == "\${AWS_EMPTY_GET:-}" ]] && { echo 0; exit 0; }
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
# external ID then a valid one, apply, ARN pasted, an empty write token then the token,
# and the API token.
answers() {
  printf '%s\n' "" "" "https://slipstream.grafana.net/" "bad id!" "ext-123" \
    "${APPLY_ANSWER:-y}" "" "" "${write_token}" "${api_token}"
}

fail=0
run() {
  rm -rf "${tmp}/store" "${tmp}/applied-with"
  : >"${tmp}/aws-calls"
  : >"${tmp}/opened"
  printf 'operator_email = "op@example.com"\ngrafana_external_id = "stale"\n' >"${tmp}/bootstrap/terraform.tfvars"
  answers | PATH="${tmp}/bin:${PATH}" "${script}" "${tmp}/bootstrap" >"${tmp}/stdout" 2>"${tmp}/stderr"
}
ok() { echo "ok   $1"; }
bad() {
  echo "FAIL $1" >&2
  fail=1
}
check_file() {
  local label="$1" file="$2" want="$3"
  if grep -qxF -- "${want}" "${file}"; then ok "${label}"; else bad "${label}: '${want}' not in ${file##*/}"; fi
}
check_no_token() {
  local label="$1" file
  for file in "${tmp}/stdout" "${tmp}/stderr" "${tmp}/aws-calls"; do
    if grep -qF -e "${write_token}" -e "${api_token}" "${file}"; then
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
check_file "role arn shown" "${tmp}/stdout" "    arn:aws:iam::111122223333:role/slipstream-grafana-cloudwatch"
check_no_token "tokens never echoed or passed as arguments"
if grep -qF "not a valid external ID" "${tmp}/stdout" && grep -qF "token is empty" "${tmp}/stdout"; then
  ok "invalid external id and empty token asked again"
else
  bad "invalid external id and empty token asked again"
fi

# Failures stop the wizard with a message naming what to do.
AWS_STS_EXIT=255 check_aborted "aws not authenticated" "aws is not authenticated"
APPLY_ANSWER=n check_aborted "apply declined" "bootstrap not applied"
AWS_FAIL_PUT=grafana-cloud/api check_aborted "failed write" "writing grafana-cloud/api failed"
check_no_token "failed write echoes no token"
AWS_EMPTY_GET=grafana-cloud/write check_aborted "secret left empty" "grafana-cloud/write holds no value"

exit "${fail}"
