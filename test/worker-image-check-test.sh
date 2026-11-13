#!/usr/bin/env bash
# Unit test for orchestration/worker-image-check.sh: kubectl is stubbed on PATH to list
# the running prefect-worker images, and the script reads a throwaway git repo whose
# commits stand in for the worker image's commit and main. It checks that a worker whose
# config equals main's passes and shows that config in the job summary, that a differing
# config fails naming the re-pin, that zero or several running worker images fail while
# several workers on one image pass, and that a worker commit missing from the history
# fails.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/orchestration/worker-image-check.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# Stub kubectl: answer only the running-worker image query, printing WORKER_IMAGES.
mkdir -p "${tmp}/bin"
cat >"${tmp}/bin/kubectl" <<'EOF'
#!/usr/bin/env bash
expected="-n prefect get pods -l app.kubernetes.io/name=prefect-worker --field-selector=status.phase=Running -o jsonpath={range .items[*]}{.spec.containers[0].image}{\"\n\"}{end}"
[[ "$*" == "${expected}" ]] || { echo "unexpected call: $*" >&2; exit 2; }
printf '%s' "${WORKER_IMAGES}"
EOF
chmod +x "${tmp}/bin/kubectl"
export PATH="${tmp}/bin:${PATH}"

# A repo with a stale config, then the config at the worker's commit, then an unrelated
# commit on top: HEAD (main) holds the worker's config and not the stale one.
repo="${tmp}/repo"
git init -q "${repo}"
git_in_repo() {
  git -C "${repo}" -c user.name=test -c user.email=test@example.com "$@"
}
mkdir -p "${repo}/bench/configs"
echo "concurrency: [1]" >"${repo}/bench/configs/sweep-grid.yaml"
git_in_repo add . && git_in_repo commit -qm stale
stale_commit="$(git_in_repo rev-parse HEAD)"
echo "concurrency: [1, 2]" >"${repo}/bench/configs/sweep-grid.yaml"
git_in_repo add . && git_in_repo commit -qm worker
worker_commit="$(git_in_repo rev-parse HEAD)"
echo "unrelated" >"${repo}/README.md"
git_in_repo add . && git_in_repo commit -qm main
image_repo="123.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
worker_image="${image_repo}:${worker_commit}"

failures=0
fail() {
  echo "FAIL $1" >&2
  failures=$((failures + 1))
}

# Print ok for a case only when it added no failure.
report() {
  [[ "${failures}" == "$1" ]] && echo "ok   $2"
  return 0
}

run_check() {
  : >"${tmp}/summary"
  (cd "${repo}" && GITHUB_STEP_SUMMARY="${tmp}/summary" \
    "${script}" prefect bench/configs/sweep-grid.yaml "Sweep grid" run-1) >"${tmp}/out" 2>"${tmp}/err"
}

before="${failures}"
if WORKER_IMAGES="${worker_image}"$'\n' run_check; then
  grep -qxF "## Sweep grid" "${tmp}/summary" || fail "matching: no heading in summary"
  grep -qF "Run \`run-1\` reads \`bench/configs/sweep-grid.yaml\` as built into worker image \`${worker_image}\`." \
    "${tmp}/summary" || fail "matching: summary does not name the run, config and image"
  grep -qxF "concurrency: [1, 2]" "${tmp}/summary" || fail "matching: summary does not show the config"
else
  fail "matching config: exited non-zero: $(cat "${tmp}/out" "${tmp}/err")"
fi
report "${before}" "matching config passes and shows it in the summary"

before="${failures}"
if WORKER_IMAGES="${image_repo}:${stale_commit}"$'\n' run_check; then
  fail "differing config: exited zero"
else
  expected="::error::the worker runs bench/configs/sweep-grid.yaml from ${stale_commit}, which differs from main's; re-pin the worker (just cluster-up) and dispatch again"
  grep -qxF "${expected}" "${tmp}/out" || fail "differing config: no re-pin message: $(cat "${tmp}/out" "${tmp}/err")"
  grep -qxF "concurrency: [1]" "${tmp}/summary" || fail "differing config: summary does not show the worker's config"
fi
report "${before}" "differing config fails naming the re-pin, after the summary"

# Each case: a name, the kubectl output, and the image list the error names.
while IFS='|' read -r name images named; do
  before="${failures}"
  if WORKER_IMAGES="$(printf '%b' "${images}")" run_check; then
    fail "${name}: exited zero"
  else
    [[ "$(cat "${tmp}/out")" == "::error::expected one running prefect-worker image, got: $(printf '%b' "${named}")" ]] ||
      fail "${name}: no one-worker message: $(cat "${tmp}/out" "${tmp}/err")"
    [[ ! -s "${tmp}/summary" ]] || fail "${name}: wrote the summary anyway"
  fi
  report "${before}" "${name} fails"
done <<CASES
zero running workers||none
two running worker images|${image_repo}:bbb\n${image_repo}:aaa\n|${image_repo}:aaa\n${image_repo}:bbb
CASES

before="${failures}"
if WORKER_IMAGES="${worker_image}"$'\n'"${worker_image}"$'\n' run_check; then
  grep -qF "as built into worker image \`${worker_image}\`." "${tmp}/summary" ||
    fail "two workers on one image: summary does not name the one image"
else
  fail "two workers on one image: exited non-zero: $(cat "${tmp}/out" "${tmp}/err")"
fi
report "${before}" "two running workers on one image pass"

before="${failures}"
if WORKER_IMAGES="${image_repo}:0000000"$'\n' run_check; then
  fail "unknown commit: exited zero"
else
  grep -qxF "::error::cannot read bench/configs/sweep-grid.yaml at worker image commit 0000000" "${tmp}/out" ||
    fail "unknown commit: no unreadable-config message: $(cat "${tmp}/out" "${tmp}/err")"
fi
report "${before}" "a worker commit main's history lacks fails"

[[ "${failures}" == 0 ]] || {
  echo "${failures} failure(s)" >&2
  exit 1
}
