#!/usr/bin/env bash
# Check the orchestration image downloads the Terraform CLI mise pins. orchestration/Dockerfile
# carries its own copy of the linux-x64 release URL and sha256 that mise.lock records for the
# mise.toml terraform pin; this fails when the Dockerfile's `ADD --checksum=` download and
# mise.lock's entry differ, so a pin bump that updates one but not the other fails CI on the
# PR that makes it. Reads the two files only: no docker, no network. MISE_LOCK and DOCKERFILE
# override the paths (test/terraform-pin-check-test.sh runs it on mutated copies).
# Usage: terraform-pin-check.sh
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mise_lock="${MISE_LOCK:-${repo_root}/mise.lock}"
dockerfile="${DOCKERFILE:-${repo_root}/orchestration/Dockerfile}"

# The value of key $1 in mise.lock's terraform linux-x64 table.
get_locked() {
  awk -v key="$1" '
    /^\[tools\.terraform\."platforms\.linux-x64"\]$/ { grab = 1; next }
    grab && /^\[/ { exit }
    grab && $1 == key && $2 == "=" { gsub(/"/, "", $3); print $3 }
  ' "${mise_lock}"
}

locked_checksum="$(get_locked checksum)"
locked_url="$(get_locked url)"
[[ -n "${locked_checksum}" && -n "${locked_url}" ]] || {
  echo "${mise_lock} records no linux-x64 terraform checksum and url" >&2
  exit 1
}

# The Dockerfile's terraform download: the one `ADD --checksum=<sha> \` line, whose next
# line is the URL. Comments are not instructions, so a value left in one is not read.
download="$(awk '
  /^ADD --checksum=sha256:[0-9a-f]+ \\$/ { sub(/^ADD --checksum=/, ""); sub(/ \\$/, ""); checksum = $0; getline; gsub(/^[[:space:]]+| \\$/, ""); print checksum, $0 }
' "${dockerfile}" | grep -F 'releases.hashicorp.com/terraform/' || true)"
[[ "$(wc -l <<<"${download}")" -eq 1 && -n "${download}" ]] || {
  echo "${dockerfile} has no single terraform download (ADD --checksum=<sha256> from releases.hashicorp.com)" >&2
  exit 1
}
read -r dockerfile_checksum dockerfile_url <<<"${download}"

[[ "${dockerfile_checksum}" == "${locked_checksum}" ]] || {
  echo "${dockerfile} verifies terraform against checksum ${dockerfile_checksum}; mise.lock records ${locked_checksum}" >&2
  exit 1
}
[[ "${dockerfile_url}" == "${locked_url}" ]] || {
  echo "${dockerfile} downloads terraform from url ${dockerfile_url}; mise.lock records ${locked_url}" >&2
  exit 1
}
