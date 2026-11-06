#!/usr/bin/env bash
# Check an orchestration image can initialise the baked bench-endpoint stack with no network.
# The image carries the Terraform CLI, the bench-endpoint stack and its providers so the worker
# can apply the stack from inside it; this asserts the CLI is the version mise.toml pins (the
# download itself is checked against mise.lock by terraform-pin-check.sh), that the stack's
# .tf files are baked, and that `terraform init` installs every provider in the stack's lock
# file. The container runs as the prefect-worker chart runs it: a read-only root (the chart's
# default container securityContext) with a writable /tmp (its emptyDir mount). It runs as
# uid 4242, a non-root uid that owns nothing in the image, standing in for the chart's 1001,
# and here with no network at all, so a provider that is not baked fails `terraform init`
# instead of downloading. The stack is copied to /tmp first because terraform cannot write
# .terraform next to the baked copy on the read-only root.
# Usage: orchestration-image-terraform-offline-check.sh <image>
set -euo pipefail

image="$1"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

pinned="$(sed -n 's/^terraform = "\(.*\)"$/\1/p' "${repo_root}/mise.toml")"
[[ -n "${pinned}" ]] || {
  echo "mise.toml pins no terraform version" >&2
  exit 1
}

# The single-quoted script runs inside the container, so its variables are the container's.
# shellcheck disable=SC2016
docker run --rm --platform linux/amd64 \
  --network none --read-only --tmpfs /tmp --user 4242:0 \
  --env PINNED="${pinned}" --env CHECKPOINT_DISABLE=1 \
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
    # init and validate both pass on an empty directory, so an unbaked stack must fail here.
    compgen -G "${stack}/*.tf" >/dev/null || {
      echo "${BENCH_ENDPOINT_STACK_DIR} holds no .tf files" >&2
      exit 1
    }
    terraform -chdir="${stack}" init -backend=false -input=false -lockfile=readonly
    locked="$(sed -n "s/^provider \"\(.*\)\" {\$/\1/p" "${stack}/.terraform.lock.hcl")"
    [[ -n "${locked}" ]] || {
      echo "the baked stack lock file names no providers" >&2
      exit 1
    }
    for provider in ${locked}; do
      [[ -d "${stack}/.terraform/providers/${provider}" ]] || {
        echo "terraform init did not install ${provider}" >&2
        exit 1
      }
    done
    terraform -chdir="${stack}" validate
  '
