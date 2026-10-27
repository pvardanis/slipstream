#!/usr/bin/env bash
# Unit test for bench/bootstrap.sh: terraform and aws are stubbed on PATH so the test
# asserts which Grafana external ID the bootstrap apply receives, never a real apply or
# AWS call. It checks that a set TF_VAR_grafana_external_id or a terraform.tfvars entry
# is used as is with no AWS read; that otherwise the ID is read from the live Grafana
# CloudWatch role named by the bootstrap output; that a bootstrap with no state yet
# applies without one, naming the setup wizard and repeating terraform's error; and that a
# failed, empty or malformed role read aborts before the apply.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/bench/bootstrap.sh"
tmp="$(mktemp -d)"
trap 'rm -r "${tmp}"' EXIT
mkdir -p "${tmp}/bin" "${tmp}/bootstrap"

# Stub terraform: init succeeds; `output -raw grafana_cloudwatch_role_arn` prints the
# role ARN, or fails when NO_STATE is set; apply records the external ID it would see
# from the environment. Any other call shape fails.
cat >"${tmp}/bin/terraform" <<EOF
#!/usr/bin/env bash
shift
case "\$1" in
  init) exit 0 ;;
  output)
    [[ "\$2 \$3" == "-raw grafana_cloudwatch_role_arn" ]] || { echo "unexpected output: \$*" >&2; exit 2; }
    [[ -n "\${NO_STATE:-}" ]] && { echo "\${OUTPUT_ERROR:-No outputs found}" >&2; exit 1; }
    echo arn:aws:iam::111122223333:role/slipstream-grafana-cloudwatch ;;
  apply) printf 'apply %s\n' "\${TF_VAR_grafana_external_id-<unset>}" >>"${tmp}/calls" ;;
  *) echo "unexpected call: \$*" >&2; exit 2 ;;
esac
EOF
# Stub aws: `iam get-role` for the named role prints ROLE_EXTERNAL_ID (default
# ext-from-role), or fails when AWS_FAIL is set; every call is recorded.
cat >"${tmp}/bin/aws" <<EOF
#!/usr/bin/env bash
printf 'aws %s\n' "\$*" >>"${tmp}/calls"
[[ "\$1 \$2 \$3 \$4" == "iam get-role --role-name slipstream-grafana-cloudwatch" ]] || { echo "unexpected call: \$*" >&2; exit 2; }
[[ -n "\${AWS_FAIL:-}" ]] && { echo "An error occurred (ExpiredToken)" >&2; exit 254; }
echo "\${ROLE_EXTERNAL_ID-ext-from-role}"
EOF
chmod +x "${tmp}/bin/terraform" "${tmp}/bin/aws"

fail=0
run() {
  : >"${tmp}/calls"
  PATH="${tmp}/bin:${PATH}" "${script}" "${tmp}/bootstrap" 2>"${tmp}/stderr"
}
pass() { echo "ok   $1"; }
flunk() {
  echo "FAIL $1: $2" >&2
  sed 's/^/     /' "${tmp}/calls" "${tmp}/stderr" >&2
  fail=1
}
# check LABEL WANT_APPLY [aws]: the script exits 0, apply sees WANT_APPLY, and aws is
# called only when the third argument is given.
check() {
  local label="$1" want="$2" want_aws="${3:-}"
  if ! run; then
    flunk "${label}" "script exited non-zero"
  elif ! grep -qxF "apply ${want}" "${tmp}/calls"; then
    flunk "${label}" "apply did not see '${want}'"
  elif [[ -z "${want_aws}" ]] && grep -q '^aws ' "${tmp}/calls"; then
    flunk "${label}" "aws called"
  elif [[ -n "${want_aws}" ]] && ! grep -q '^aws iam get-role' "${tmp}/calls"; then
    flunk "${label}" "role not read"
  else
    pass "${label}"
  fi
}
check_aborted() {
  local label="$1" want_err="$2"
  if run; then
    flunk "${label}" "script exited 0"
  elif grep -q '^apply' "${tmp}/calls"; then
    flunk "${label}" "apply ran before the abort"
  elif ! grep -qF -- "${want_err}" "${tmp}/stderr"; then
    flunk "${label}" "stderr lacks '${want_err}'"
  else
    pass "${label}"
  fi
}

# A set variable is used as is, with no AWS read.
TF_VAR_grafana_external_id=ext-from-env check "env var used as is" ext-from-env
# A tfvars entry is used as is (terraform reads it over the environment), with no AWS read.
echo 'grafana_external_id = "ext-from-file"' >"${tmp}/bootstrap/terraform.tfvars"
check "tfvars entry used as is" "<unset>"
# A tfvars file without the entry falls through to the role.
echo 'operator_email = "a@b.c"' >"${tmp}/bootstrap/terraform.tfvars"
check "tfvars without the entry reads the role" ext-from-role aws
rm "${tmp}/bootstrap/terraform.tfvars"
# Neither: the ID is read from the live role.
check "read from the live role" ext-from-role aws

# No bootstrap state yet: apply runs without the ID, and the wizard is named.
if NO_STATE=1 run && grep -qxF "apply <unset>" "${tmp}/calls" && grep -qF "just grafana-cloud-setup" "${tmp}/stderr"; then
  pass "no state applies without the ID and names the wizard"
else
  flunk "no state applies without the ID and names the wizard" "unexpected result"
fi

# A failed or empty role read stops before the apply rather than prompting or passing "".
AWS_FAIL=1 check_aborted "failed role read" "cannot read the external ID"
ROLE_EXTERNAL_ID="" check_aborted "empty role read" "cannot read the external ID"
ROLE_EXTERNAL_ID=None check_aborted "role without the condition" "cannot read the external ID"
# A list-valued condition prints tab-separated IDs; written back, it would be one bad ID.
ROLE_EXTERNAL_ID=$'ext-a\text-b' check_aborted "role with several IDs" "is not one external ID"

# A failed output read repeats terraform's own error, so a cause other than missing state
# reads as itself.
if NO_STATE=1 OUTPUT_ERROR="Error: ExpiredToken" run && grep -qF "Error: ExpiredToken" "${tmp}/stderr"; then
  pass "failed output read repeats terraform's error"
else
  flunk "failed output read repeats terraform's error" "error not repeated"
fi

exit "${fail}"
