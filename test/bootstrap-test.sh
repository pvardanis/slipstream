#!/usr/bin/env bash
# Unit test for bench/bootstrap.sh: terraform and aws are stubbed on PATH so the test
# asserts which Grafana external ID and operator email the bootstrap apply receives, never
# a real apply or AWS call. For each of the two, it checks that a set TF_VAR_ or an
# uncommented terraform.tfvars entry is used as is with no AWS read; that otherwise it is
# read from the live resource the bootstrap output names (the Grafana CloudWatch role's
# trust policy, the spend-leak topic's email subscription); that a bootstrap with no state
# yet, a state without one output, or a topic with no email subscription applies without
# the value, so terraform prompts for it; and that any other failed output read, or a
# failed, empty or malformed AWS read, aborts before the apply. The apply stays interactive
# so that prompt can happen.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/bench/bootstrap.sh"
tmp="$(mktemp -d)"
trap 'rm -r "${tmp}"' EXIT
mkdir -p "${tmp}/bin" "${tmp}/bootstrap"

# Stub terraform, matching the real CLI: init succeeds; `output -no-color -raw` prints the
# role or topic ARN, prints nothing and succeeds when NO_STATE is set, fails with
# terraform's not-found error for MISSING_OUTPUT, and fails with OUTPUT_ERROR when that is
# set; apply records the external ID and email it would see from the environment, and
# fails if run with -input=false. Any other call shape fails.
cat >"${tmp}/bin/terraform" <<EOF
#!/usr/bin/env bash
shift
case "\$1" in
  init) exit 0 ;;
  output)
    [[ "\$2 \$3" == "-no-color -raw" ]] || { echo "unexpected output: \$*" >&2; exit 2; }
    [[ -n "\${OUTPUT_ERROR:-}" ]] && { echo "\${OUTPUT_ERROR}" >&2; exit 1; }
    [[ -n "\${NO_STATE:-}" ]] && exit 0
    [[ "\$4" == "\${MISSING_OUTPUT:-}" ]] && { echo "Error: Output \"\$4\" not found" >&2; exit 1; }
    case "\$4" in
      grafana_cloudwatch_role_arn) echo arn:aws:iam::111122223333:role/slipstream-grafana-cloudwatch ;;
      spend_leak_topic_arn) echo arn:aws:sns:eu-west-1:111122223333:slipstream-spend-leak ;;
      *) echo "unexpected output: \$*" >&2; exit 2 ;;
    esac ;;
  apply)
    [[ " \$* " == *" -input=false "* ]] && { echo "apply must stay interactive" >&2; exit 2; }
    printf 'apply %s %s\n' "\${TF_VAR_grafana_external_id-<unset>}" "\${TF_VAR_operator_email-<unset>}" >>"${tmp}/calls" ;;
  *) echo "unexpected call: \$*" >&2; exit 2 ;;
esac
EOF
# Stub aws: `iam get-role` for the named role prints ROLE_EXTERNAL_ID (default
# ext-from-role), or fails when AWS_FAIL is set; `sns list-subscriptions-by-topic` for the
# named topic prints TOPIC_EMAIL (default topic@example.com), or fails when SNS_FAIL is
# set. Every call is recorded.
cat >"${tmp}/bin/aws" <<EOF
#!/usr/bin/env bash
printf 'aws %s\n' "\$*" >>"${tmp}/calls"
case "\$1 \$2 \$3 \$4" in
  "iam get-role --role-name slipstream-grafana-cloudwatch")
    [[ -n "\${AWS_FAIL:-}" ]] && { echo "An error occurred (ExpiredToken)" >&2; exit 254; }
    echo "\${ROLE_EXTERNAL_ID-ext-from-role}" ;;
  "sns list-subscriptions-by-topic --topic-arn arn:aws:sns:eu-west-1:111122223333:slipstream-spend-leak")
    [[ -n "\${SNS_FAIL:-}" ]] && { echo "An error occurred (ExpiredToken)" >&2; exit 254; }
    echo "\${TOPIC_EMAIL-topic@example.com}" ;;
  *) echo "unexpected call: \$*" >&2; exit 2 ;;
esac
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
# aws_reads prints the aws subcommands called, space-separated, in call order.
aws_reads() { awk '$1 == "aws" { printf "%s%s", sep, $3; sep = " " }' "${tmp}/calls"; }
# check LABEL WANT_APPLY WANT_AWS: the script exits 0, apply sees WANT_APPLY (the external
# ID then the email), and the aws subcommands called are exactly WANT_AWS.
check() {
  local label="$1" want="$2" want_aws="$3"
  if ! run; then
    flunk "${label}" "script exited non-zero"
  elif ! grep -qxF "apply ${want}" "${tmp}/calls"; then
    flunk "${label}" "apply did not see '${want}'"
  elif [[ "$(aws_reads)" != "${want_aws}" ]]; then
    flunk "${label}" "aws reads were '$(aws_reads)', not '${want_aws}'"
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

# Set variables are used as is, with no AWS read.
TF_VAR_grafana_external_id=ext-from-env TF_VAR_operator_email=env@example.com \
  check "env vars used as is" "ext-from-env env@example.com" ""
# tfvars entries are used as is (terraform reads them over the environment), with no AWS read.
printf 'grafana_external_id = "ext-from-file"\noperator_email = "file@example.com"\n' >"${tmp}/bootstrap/terraform.tfvars"
check "tfvars entries used as is" "<unset> <unset>" ""
# A commented-out entry does not set the variable.
printf '# grafana_external_id = "ext-from-file"\n# operator_email = "file@example.com"\n' >"${tmp}/bootstrap/terraform.tfvars"
check "commented tfvars entries read both" "ext-from-role topic@example.com" "get-role list-subscriptions-by-topic"
# Each variable is looked up on its own: a tfvars file with one entry reads only the other.
echo 'operator_email = "file@example.com"' >"${tmp}/bootstrap/terraform.tfvars"
check "tfvars email entry reads only the role" "ext-from-role <unset>" "get-role"
echo 'grafana_external_id = "ext-from-file"' >"${tmp}/bootstrap/terraform.tfvars"
check "tfvars ID entry reads only the topic" "<unset> topic@example.com" "list-subscriptions-by-topic"
rm "${tmp}/bootstrap/terraform.tfvars"
TF_VAR_operator_email=env@example.com check "email env var reads only the role" "ext-from-role env@example.com" "get-role"
# Neither: both are read from the live resources.
check "read from the live role and topic" "ext-from-role topic@example.com" "get-role list-subscriptions-by-topic"

# No bootstrap state yet: apply runs without either, and the wizard is named.
if NO_STATE=1 run && grep -qxF "apply <unset> <unset>" "${tmp}/calls" && grep -qF "just grafana-cloud-setup" "${tmp}/stderr" &&
  grep -qF "prompts for grafana_external_id" "${tmp}/stderr" && grep -qF "prompts for operator_email" "${tmp}/stderr"; then
  pass "no state applies without them and names the wizard"
else
  flunk "no state applies without them and names the wizard" "unexpected result"
fi

# A state without the topic output (it gains the output on this apply) still reads the ID,
# applies without the email so terraform prompts for it, and says the apply adds the output.
if MISSING_OUTPUT=spend_leak_topic_arn run && grep -qxF "apply ext-from-role <unset>" "${tmp}/calls" &&
  grep -qF "gains it on this apply" "${tmp}/stderr"; then
  pass "state without the topic output prompts for the email once"
else
  flunk "state without the topic output prompts for the email once" "unexpected result"
fi
# The two lookups are independent: a state without the role output still reads the email.
if MISSING_OUTPUT=grafana_cloudwatch_role_arn run && grep -qxF "apply <unset> topic@example.com" "${tmp}/calls"; then
  pass "state without the role output still reads the email"
else
  flunk "state without the role output still reads the email" "unexpected result"
fi

# A topic with no email subscription (the address unsubscribed) applies without the email,
# so terraform prompts for it and the apply subscribes it again.
if TOPIC_EMAIL="" run && grep -qxF "apply ext-from-role <unset>" "${tmp}/calls" &&
  grep -qF "has no email subscription" "${tmp}/stderr"; then
  pass "topic without an email subscription prompts for it"
else
  flunk "topic without an email subscription prompts for it" "unexpected result"
fi

# A failed or empty read stops before the apply rather than prompting or passing "".
AWS_FAIL=1 check_aborted "failed role read" "cannot read the external ID"
ROLE_EXTERNAL_ID="" check_aborted "empty role read" "cannot read the external ID"
ROLE_EXTERNAL_ID=None check_aborted "role without the condition" "cannot read the external ID"
# A list-valued condition prints tab-separated IDs; written back, it would be one bad ID.
ROLE_EXTERNAL_ID=$'ext-a\text-b' check_aborted "role with several IDs" "is not one external ID"
SNS_FAIL=1 check_aborted "failed topic read" "cannot read the operator email"
# Several email subscriptions print tab-separated; written back, it would be one bad address.
TOPIC_EMAIL=$'a@example.com\tb@example.com' check_aborted "topic with several emails" "not one email address"

# Any other failed output read (credentials, state access, a held lock) stops with
# terraform's own error rather than applying with a prompt that hides it.
OUTPUT_ERROR="Error: ExpiredToken" check_aborted "failed output read stops with terraform's error" "Error: ExpiredToken"

exit "${fail}"
