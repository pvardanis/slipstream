#!/usr/bin/env bash
# Unit test for bench/build-image.sh: docker is stubbed on PATH so the test
# asserts the command the script assembles, never a real build. It checks that
# the model.yaml-derived build args are passed, that each image ref becomes a
# `-t` flag, and that passing no refs is safe (the empty-array trap under
# `set -u` on macOS bash 3.2).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/bench/build-image.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# Stub docker: record its argv, one per line, and exit 0. Prepending its dir to
# PATH shadows the real docker for the script's build call.
mkdir -p "${tmp}/bin"
cat >"${tmp}/bin/docker" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" >"${tmp}/argv"
EOF
chmod +x "${tmp}/bin/docker"

fail=0
run() { PATH="${tmp}/bin:${PATH}" "${script}" "$@"; }
check() {
  local label="$1" want="$2"
  if grep -qxF -- "${want}" "${tmp}/argv"; then
    echo "ok   ${label}"
  else
    echo "FAIL ${label}: '${want}' not in docker argv" >&2
    fail=1
  fi
}
check_absent() {
  local label="$1" needle="$2"
  if grep -qF -- "${needle}" "${tmp}/argv"; then
    echo "FAIL ${label}: '${needle}' should not be in docker argv" >&2
    fail=1
  else
    echo "ok   ${label}"
  fi
}

# No refs: build for amd64 with the served model's args and no -t flag. This is
# the ci.yml PR check's call, and the case that trips an unguarded empty array.
run
check "platform" "linux/amd64"
check "model build-arg" "MODEL=Qwen/Qwen3-8B-AWQ"
check "revision build-arg" "REVISION=4da05a8edb55c6046cce958586c33b61da07bb79"
check "dockerfile flag" "-f"
check_absent "no tag when no refs" "-t"

# Two refs: each becomes its own -t flag (bench-image.yml's sha + main tags).
run repo:sha repo:main
check "first tag flag present" "-t"
check "first ref tagged" "repo:sha"
check "second ref tagged" "repo:main"
if [[ "$(grep -cxF -- '-t' "${tmp}/argv")" != "2" ]]; then
  echo "FAIL two refs: expected exactly two -t flags" >&2
  fail=1
else
  echo "ok   two -t flags for two refs"
fi

exit "${fail}"
