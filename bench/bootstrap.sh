#!/usr/bin/env bash
# Apply the bootstrap stack. Its grafana_external_id and operator_email variables have no
# default; each one that neither its TF_VAR_ nor the gitignored terraform.tfvars sets (a
# fresh worktree has no tfvars file) is read back from the live resource holding it: the
# external ID from the Grafana CloudWatch role's trust policy, the email from the
# spend-leak topic's email subscription. A rerun then applies without prompting. A
# bootstrap with no state yet has neither resource to read, and an unsubscribed address
# leaves no subscription, so terraform prompts for what is missing; `just
# grafana-cloud-setup` writes both instead.
# Usage: bootstrap.sh <bootstrap-dir>
set -euo pipefail

# shellcheck source=bench/bootstrap-values.sh
source "${BASH_SOURCE[0]%/*}/bootstrap-values.sh"

bootstrap_dir="$1"
output_error="$(mktemp)"
trap 'rm -f "${output_error}"' EXIT

# is_set NAME succeeds when TF_VAR_NAME or a terraform.tfvars entry sets the variable.
is_set() {
  local env_name="TF_VAR_$1"
  [[ -n "${!env_name:-}" ]] ||
    grep -qE "^[[:space:]]*$1[[:space:]]*=" "${bootstrap_dir}/terraform.tfvars" 2>/dev/null
}

# read_output NAME VARIABLE sets output_value to the bootstrap output NAME. A bootstrap
# with no state yet prints nothing, and a state the output is not in until this apply
# fails with terraform's not-found error; either way it says terraform prompts for
# VARIABLE and returns 1. Any other failure (credentials, state access, a held lock) stops
# here with terraform's error, so the apply never starts on a prompt that hides it.
read_output() {
  if output_value="$(terraform -chdir="${bootstrap_dir}" output -no-color -raw "$1" 2>"${output_error}")"; then
    [[ -n "${output_value}" ]] && return 0
  elif ! grep -qF "Output \"$1\" not found" "${output_error}"; then
    cat "${output_error}" >&2
    echo "cannot read the bootstrap output $1" >&2
    exit 1
  fi
  echo "the bootstrap state has no output $1, so terraform prompts for $2 (with no bootstrap state yet, run \`just grafana-cloud-setup\` instead; a state without the output gains it on this apply)" >&2
  return 1
}

# fail_lookup VARIABLE MESSAGE stops before the apply, naming where VARIABLE can be set.
fail_lookup() {
  echo "$2; set $1 in ${bootstrap_dir}/terraform.tfvars or TF_VAR_$1" >&2
  exit 1
}

terraform -chdir="${bootstrap_dir}" init -input=false

if ! is_set grafana_external_id && read_output grafana_cloudwatch_role_arn grafana_external_id; then
  role="${output_value##*/}"
  # The aws error stays on stderr, so a credentials or access failure reads as itself.
  # --output text prints None when the role has no such condition.
  if ! external_id="$(aws iam get-role --role-name "${role}" \
    --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals."sts:ExternalId"' \
    --output text)" || [[ -z "${external_id}" || "${external_id}" == None ]]; then
    fail_lookup grafana_external_id "cannot read the external ID from role ${role}"
  fi
  # A list-valued condition prints its IDs tab-separated, which is not one ID.
  is_external_id "${external_id}" ||
    fail_lookup grafana_external_id "the ExternalId condition on role ${role} is not one external ID"
  export TF_VAR_grafana_external_id="${external_id}"
fi

if ! is_set operator_email && read_output spend_leak_topic_arn operator_email; then
  topic_arn="${output_value}"
  # A pending subscription still lists its endpoint, so an unconfirmed address is read too.
  if ! email="$(aws sns list-subscriptions-by-topic --topic-arn "${topic_arn}" \
    --query "Subscriptions[?Protocol=='email'].Endpoint" --output text)"; then
    fail_lookup operator_email "cannot read the operator email from topic ${topic_arn##*:}"
  fi
  if [[ -z "${email}" ]]; then
    # An unsubscribed address leaves nothing to read back; the apply subscribes the
    # address terraform prompts for.
    echo "topic ${topic_arn##*:} has no email subscription, so terraform prompts for operator_email" >&2
  else
    # Several email subscriptions print tab-separated, which is not one address.
    is_email "${email}" ||
      fail_lookup operator_email "the email subscriptions on topic ${topic_arn##*:} are not one email address"
    export TF_VAR_operator_email="${email}"
  fi
fi

terraform -chdir="${bootstrap_dir}" apply -auto-approve
