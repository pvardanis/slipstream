#!/usr/bin/env bash
# Unit test for orchestration/terraform-pin-check.sh: runs it against the repo's real
# mise.lock and orchestration/Dockerfile, which must pass, and against copies mutated the
# ways a Terraform pin bump can drift, which must each fail naming the drift. No docker,
# no network: the script only reads the two files.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/orchestration/terraform-pin-check.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

fail=0

# Run the script on copies of the two files with sed expression $2 applied to the file
# named $1 (lock or dockerfile); with an empty $1 the copies are unchanged.
run_check() {
  local target="$1" expression="${2:-}"
  cp "${repo_root}/mise.lock" "${tmp}/mise.lock"
  cp "${repo_root}/orchestration/Dockerfile" "${tmp}/Dockerfile"
  local file=""
  case "${target}" in
  lock) file="${tmp}/mise.lock" ;;
  dockerfile) file="${tmp}/Dockerfile" ;;
  esac
  if [[ -n "${file}" ]]; then
    sed "${expression}" "${file}" >"${file}.mutated"
    if cmp -s "${file}" "${file}.mutated"; then
      echo "mutation changed nothing" >"${tmp}/err"
      return 3
    fi
    mv "${file}.mutated" "${file}"
  fi
  MISE_LOCK="${tmp}/mise.lock" DOCKERFILE="${tmp}/Dockerfile" "${script}" >"${tmp}/out" 2>"${tmp}/err"
}

if run_check ""; then
  echo "ok   the repo's mise.lock and Dockerfile agree"
else
  echo "FAIL the repo's files: $(cat "${tmp}/err")" >&2
  fail=1
fi

# Each case: a label, the file to mutate, the sed expression, and text the error must name.
expect_failure() {
  local label="$1" target="$2" expression="$3" message="$4" status=0
  run_check "${target}" "${expression}" || status=$?
  if [[ "${status}" == 3 ]]; then
    echo "FAIL ${label}: the mutation no longer changes the file; update it" >&2
    fail=1
  elif [[ "${status}" == 0 ]]; then
    echo "FAIL ${label}: passed" >&2
    fail=1
  elif ! grep -qF -- "${message}" "${tmp}/err"; then
    echo "FAIL ${label}: error does not name '${message}': $(cat "${tmp}/err")" >&2
    fail=1
  else
    echo "ok   ${label}"
  fi
}

expect_failure "a Dockerfile checksum that differs from mise.lock" dockerfile \
  's/^ADD --checksum=sha256:[0-9a-f]\{4\}/ADD --checksum=sha256:0000/' "checksum"
expect_failure "a Dockerfile URL that differs from mise.lock" dockerfile \
  's|releases.hashicorp.com/terraform/\([0-9.]*\)/|releases.hashicorp.com/terraform/9.9.9/|' "url"
expect_failure "mise.lock's checksum changed without the Dockerfile" lock \
  '/terraform."platforms.linux-x64"\]/,/^url/s/^checksum = "sha256:[0-9a-f]\{4\}/checksum = "sha256:0000/' "checksum"
expect_failure "a stale checksum left only in a Dockerfile comment" dockerfile \
  '/^ADD --checksum=/{h;s/^ADD --checksum=\(.*\) \\$/# \1/;p;x;s/sha256:[0-9a-f]*/sha256:1111111111111111111111111111111111111111111111111111111111111111/;}' "checksum"
expect_failure "mise.lock without a linux-x64 terraform entry" lock \
  's/^\[tools\.terraform\."platforms\.linux-x64"\]/[tools.terraform."platforms.linux-arm64"]/' "linux-x64"
expect_failure "an empty checksum in mise.lock" lock \
  '/terraform."platforms.linux-x64"\]/,/^url/s/^checksum = .*/checksum = ""/' "linux-x64"
expect_failure "a Dockerfile with no terraform download" dockerfile \
  '/releases.hashicorp.com\/terraform/d' "terraform download"

[[ "${fail}" == 0 ]]
