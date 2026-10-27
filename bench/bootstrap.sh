#!/usr/bin/env bash
# Apply the bootstrap stack. Its grafana_external_id variable has no default; when neither
# TF_VAR_grafana_external_id nor the gitignored terraform.tfvars sets it (a fresh worktree
# has no tfvars file), the ID is read back from the live Grafana CloudWatch role's trust
# policy, so a rerun applies without prompting. A bootstrap with no state yet has no role
# to read, so terraform prompts for it; `just grafana-cloud-setup` writes the ID instead.
# Usage: bootstrap.sh <bootstrap-dir>
set -euo pipefail

bootstrap_dir="$1"

terraform -chdir="${bootstrap_dir}" init -input=false

if [[ -z "${TF_VAR_grafana_external_id:-}" ]] &&
  ! grep -qE '^[[:space:]]*grafana_external_id[[:space:]]*=' "${bootstrap_dir}/terraform.tfvars" 2>/dev/null; then
  output_error="$(mktemp)"
  trap 'rm "${output_error}"' EXIT
  if role_arn="$(terraform -chdir="${bootstrap_dir}" output -raw grafana_cloudwatch_role_arn 2>"${output_error}")" &&
    [[ -n "${role_arn}" ]]; then
    role="${role_arn##*/}"
    # The aws error stays on stderr, so a credentials or access failure reads as itself.
    # --output text prints None when the role has no such condition.
    if ! external_id="$(aws iam get-role --role-name "${role}" \
      --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals."sts:ExternalId"' \
      --output text)" || [[ -z "${external_id}" || "${external_id}" == None ]]; then
      echo "cannot read the external ID from role ${role}; set grafana_external_id in ${bootstrap_dir}/terraform.tfvars or TF_VAR_grafana_external_id" >&2
      exit 1
    fi
    # A list-valued condition prints its IDs tab-separated; AWS external IDs are 2-1224
    # characters from this set, so anything else is not one ID.
    if ((${#external_id} < 2 || ${#external_id} > 1224)) || [[ ! "${external_id}" =~ ^[[:alnum:]_+=,.@:/-]+$ ]]; then
      echo "the ExternalId condition on role ${role} is not one external ID; set grafana_external_id in ${bootstrap_dir}/terraform.tfvars or TF_VAR_grafana_external_id" >&2
      exit 1
    fi
    export TF_VAR_grafana_external_id="${external_id}"
  else
    echo "cannot read the bootstrap output grafana_cloudwatch_role_arn, so grafana_external_id is not looked up (with no bootstrap state yet, run \`just grafana-cloud-setup\` to set it):" >&2
    cat "${output_error}" >&2
  fi
fi

terraform -chdir="${bootstrap_dir}" apply -auto-approve
