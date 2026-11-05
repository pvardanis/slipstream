#!/usr/bin/env bash
# Check an orchestration image can initialise the baked bench-endpoint stack with no network.
# The worker applies terraform/bench-endpoint from inside this image, so the image must carry
# the Terraform CLI at the version mise.toml pins, the stack's files, and its providers. The
# container runs the way the prefect-worker chart's default container securityContext runs
# it, as an arbitrary uid on a read-only root filesystem with a writable /tmp, and here with no
# network at all, so a provider that is not baked fails `terraform init` instead of
# downloading. The stack is copied to /tmp first because the read-only root leaves no room
# for .terraform next to the baked copy.
# Usage: orchestration-image-terraform-offline-check.sh <image>
set -euo pipefail

image="$1"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

pinned="$(sed -n 's/^terraform = "\(.*\)"$/\1/p' "${repo_root}/mise.toml")"
[[ -n "${pinned}" ]] || {
  echo "mise.toml pins no terraform version" >&2
  exit 1
}

# The Dockerfile downloads the CLI from mise.lock's linux-x64 URL and verifies it against the
# checksum mise.lock records there, so a pin bump that updates one file but not the other fails
# here, before the image is run.
locked="$(awk '/^\[tools\.terraform\."platforms\.linux-x64"\]/ { grab = 1; next }
  grab && /^\[/ { exit }
  grab && /^(checksum|url) = / { gsub(/"/, "", $3); print $3 }' "${repo_root}/mise.lock")"
[[ "$(wc -l <<<"${locked}")" -eq 2 ]] || {
  echo "mise.lock records no linux-x64 terraform checksum and url" >&2
  exit 1
}
while read -r value; do
  grep -qF "${value}" "${repo_root}/orchestration/Dockerfile" || {
    echo "orchestration/Dockerfile does not download terraform with mise.lock's ${value}" >&2
    exit 1
  }
done <<<"${locked}"

# The single-quoted script runs inside the container, so its variables are the container's.
# shellcheck disable=SC2016
docker run --rm --platform linux/amd64 \
  --network none --read-only --tmpfs /tmp --user 4242:0 \
  --env PINNED="${pinned}" \
  "${image}" bash -euo pipefail -c '
    command -v terraform >/dev/null || {
      echo "image has no terraform on its PATH" >&2
      exit 1
    }
    actual="$(terraform version -json | python -c "import json, sys; print(json.load(sys.stdin)[\"terraform_version\"])")"
    [[ "${actual}" == "${PINNED}" ]] || {
      echo "image terraform is ${actual}, mise.toml pins ${PINNED}" >&2
      exit 1
    }
    stack="$(mktemp -d)"
    cp -R "${BENCH_ENDPOINT_STACK_DIR}/." "${stack}"
    terraform -chdir="${stack}" init -backend=false -input=false -lockfile=readonly
    terraform -chdir="${stack}" validate
  '
