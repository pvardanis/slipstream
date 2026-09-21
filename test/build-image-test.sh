#!/usr/bin/env bash
# Unit test for bench/build-image.sh: docker is stubbed on PATH so the test
# asserts the command the script assembles, never a real build. It checks that
# the model.yaml-derived build args are passed, that each image ref becomes a
# `-t` flag (none, one, two), that passing no refs is safe (the empty-array trap
# under `set -u` on macOS bash 3.2), and that a failure in either image-tag.sh or
# docker aborts the script rather than being swallowed.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/bench/build-image.sh"
image_tag="${repo_root}/bench/image-tag.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# Stub docker: append its argv (one arg per line) so a stray second invocation is
# caught rather than overwriting the first, and exit with ${DOCKER_EXIT:-0} so the
# docker-failure path is testable. Prepending its dir to PATH shadows real docker.
mkdir -p "${tmp}/bin"
cat >"${tmp}/bin/docker" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" >>"${tmp}/argv"
exit "\${DOCKER_EXIT:-0}"
EOF
chmod +x "${tmp}/bin/docker"

fail=0
# Truncate the argv record before each run so an append never bleeds across runs.
run() {
  : >"${tmp}/argv"
  PATH="${tmp}/bin:${PATH}" "${script}" "$@"
}
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
check_tag_count() {
  local label="$1" want="$2" got
  got="$(grep -cxF -- '-t' "${tmp}/argv" || true)"
  if [[ "${got}" == "${want}" ]]; then
    echo "ok   ${label}"
  else
    echo "FAIL ${label}: want ${want} -t flags, got ${got}" >&2
    fail=1
  fi
}

# The served model's build args come from image-tag.sh; derive the expected
# values from it too, so the assertion tracks model.yaml rather than churning on
# every model bump — it proves build-image.sh passes them through, not the pin.
want_model="$("${image_tag}" hf-id)"
want_revision="$("${image_tag}" revision)"

# No refs: build for amd64 with the served model's args and no -t flag. This is
# the ci.yml PR check's call, and the case that trips an unguarded empty array.
run
check "platform" "linux/amd64"
check "model build-arg" "MODEL=${want_model}"
check "revision build-arg" "REVISION=${want_revision}"
check "dockerfile flag" "-f"
check_absent "no tag when no refs" "-t"
check_tag_count "no -t flag for no refs" 0

# One ref: the local `just bench-image` recipe's call (the single -sha tag).
run repo:sha
check "single ref tagged" "repo:sha"
check_tag_count "one -t flag for one ref" 1

# Two refs: each becomes its own -t flag (bench-image.yml's sha + main tags).
run repo:sha repo:main
check "first ref tagged" "repo:sha"
check "second ref tagged" "repo:main"
check_tag_count "two -t flags for two refs" 2

# An image-tag.sh failure must abort before docker runs, not build with empty
# MODEL/REVISION: build-image.sh assigns them before use precisely so `set -e`
# fires here. A model file missing hfId makes image-tag.sh exit non-zero.
cat >"${tmp}/no-hfid.yaml" <<'YAML'
model:
  revision: main
YAML
: >"${tmp}/argv"
if MODEL_FILE="${tmp}/no-hfid.yaml" PATH="${tmp}/bin:${PATH}" "${script}" >/dev/null 2>&1; then
  echo "FAIL image-tag.sh failure: expected non-zero exit" >&2
  fail=1
else
  echo "ok   image-tag.sh failure aborts the build"
fi
if [[ -s "${tmp}/argv" ]]; then
  echo "FAIL image-tag.sh failure: docker was called despite the abort" >&2
  fail=1
else
  echo "ok   docker not called when image-tag.sh fails"
fi

# A docker build failure must propagate, not be swallowed.
if DOCKER_EXIT=1 PATH="${tmp}/bin:${PATH}" "${script}" >/dev/null 2>&1; then
  echo "FAIL docker failure: expected non-zero exit" >&2
  fail=1
else
  echo "ok   docker failure propagates"
fi

exit "${fail}"
