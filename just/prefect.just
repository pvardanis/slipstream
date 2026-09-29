# Prefect Cloud lifecycle: wire the hosted workspace's work pool and publish its
# API URL + key to Secrets Manager, or tear that wiring down. The Cloud account and
# its single Hobby workspace are a one-time manual bootstrap (see prefect-cloud-up);
# these recipes manage only what Terraform can — the work pool and the secret — and
# read the account, workspace and key from the active `prefect cloud login` profile.

# Print the Prefect API URL from the active login profile, or abort. The URL carries
# the account and workspace ids the up/down recipes parse out of it. A failed
# `prefect config view` (CLI missing, crash, corrupt config) surfaces its own stderr;
# a clean run that simply lacks the URL is the not-logged-in case.
_prefect-api-url:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! raw="$(prefect config view 2>&1)"; then
      echo "prefect config view failed: ${raw}" >&2; exit 1
    fi
    url="$(printf '%s\n' "${raw}" | sed -n "s/^PREFECT_API_URL='\(.*\)'.*/\1/p")"
    [[ -n "${url}" ]] || { echo "no PREFECT_API_URL in the active profile; run 'prefect cloud login' first" >&2; exit 1; }
    printf '%s' "${url}"

# Print the Prefect API key from the active login profile, or abort. Consumed only
# through command substitution into TF_VAR_prefect_api_key, never echoed to a terminal.
# On a `prefect config view` failure the captured output holds its error, not the key,
# so surfacing it is safe; a clean run missing the key is the not-logged-in case.
_prefect-api-key:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! raw="$(prefect config view --show-secrets 2>&1)"; then
      echo "prefect config view failed: ${raw}" >&2; exit 1
    fi
    key="$(printf '%s\n' "${raw}" | sed -n "s/^PREFECT_API_KEY='\(.*\)'.*/\1/p")"
    [[ -n "${key}" ]] || { echo "no PREFECT_API_KEY in the active profile; run 'prefect cloud login' first" >&2; exit 1; }
    printf '%s' "${key}"

# Print "account workspace bucket": the two ids parsed out of the profile's API URL
# (.../accounts/<account>/workspaces/<workspace>) and the state bucket from bootstrap.
# The up/down recipes read all three from here so the lookup and parse live once.
_prefect-cloud-ctx: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    api_url="$(just _prefect-api-url)"
    [[ "${api_url}" =~ accounts/([0-9a-fA-F-]+)/workspaces/([0-9a-fA-F-]+) ]] \
      || { echo "could not parse account/workspace ids from PREFECT_API_URL: ${api_url}" >&2; exit 1; }
    printf '%s %s %s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${bucket}"

# Create the Kubernetes work pool in the Hobby workspace and publish PREFECT_API_URL
# + PREFECT_API_KEY to Secrets Manager for the EKS worker and CI
# (ADR-0015-unattended-sweep-cloud-server-and-parent-flow).
#
# One-time manual bootstrap first (the Hobby tier has no API for these):
#   1. Sign up at https://app.prefect.cloud (free, no card). A default workspace is
#      created for you; Hobby caps the account at that one workspace.
#   2. Mint a personal API key (profile menu -> API keys -> Create). Hobby has no
#      service accounts, so a personal key is the auth the worker and CI use.
#   3. Log the CLI in once: `prefect cloud login -k <key>`.
# This recipe then reads the account, workspace and key from that login profile — no
# environment variables to set.
[group('prefect')]
prefect-cloud-up:
    #!/usr/bin/env bash
    set -euo pipefail
    # Capture into a var first so a failed lookup aborts (set -e) rather than leaving
    # `read` with empty ids that would silently reach terraform (see `cluster-up`).
    ctx="$(just _prefect-cloud-ctx)"
    read -r account workspace bucket <<< "${ctx}"
    # Pass the api-key through the environment, not `-var`, so the secret never lands
    # in the terraform process argv (visible via `ps`) or shell history.
    TF_VAR_prefect_api_key="$(just _prefect-api-key)"
    export TF_VAR_prefect_api_key
    terraform -chdir={{ prefect_cloud_dir }} init -input=false -backend-config="bucket=${bucket}"
    terraform -chdir={{ prefect_cloud_dir }} apply -auto-approve \
      -var="prefect_account_id=${account}" \
      -var="workspace_id=${workspace}"
    terraform -chdir={{ prefect_cloud_dir }} output

# Remove the work pool and the published secret. The Hobby workspace and account
# survive — they are the manual bootstrap, not Terraform's to delete. Destroy still
# calls the Prefect API to drop the work pool, so it reads the same login profile.
[group('prefect')]
prefect-cloud-down:
    #!/usr/bin/env bash
    set -euo pipefail
    ctx="$(just _prefect-cloud-ctx)"
    read -r account workspace bucket <<< "${ctx}"
    TF_VAR_prefect_api_key="$(just _prefect-api-key)"
    export TF_VAR_prefect_api_key
    terraform -chdir={{ prefect_cloud_dir }} init -input=false -backend-config="bucket=${bucket}"
    terraform -chdir={{ prefect_cloud_dir }} destroy -auto-approve \
      -var="prefect_account_id=${account}" \
      -var="workspace_id=${workspace}"
