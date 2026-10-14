#!/usr/bin/env bash
# Unit test for bench/orchestration-image-check.sh: terraform and aws are stubbed on
# PATH so the test asserts the ECR lookup the script makes from the bootstrap outputs,
# never a real AWS call. It checks that a published :main passes, that a missing one
# fails naming the publish step, and that an empty bootstrap output aborts before any
# lookup.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/bench/orchestration-image-check.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# Stub terraform: answer `-chdir=bootstrap output -raw <name>` from a fixed table;
# EMPTY_OUTPUT names one output that resolves to exit-0 empty stdout.
mkdir -p "${tmp}/bin"
cat >"${tmp}/bin/terraform" <<'EOF'
#!/usr/bin/env bash
[[ "$#" == 4 && "$2" == output && "$3" == -raw ]] || { echo "unexpected call: $*" >&2; exit 2; }
[[ "$4" == "${EMPTY_OUTPUT:-}" ]] && exit 0
case "${1#-chdir=}/$4" in
  bootstrap/region) echo eu-west-1 ;;
  bootstrap/orchestration_image_repo_url) echo 123.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration ;;
  *) echo "unknown output: $*" >&2; exit 2 ;;
esac
EOF
# Stub aws: record the call; IMAGE_MISSING makes describe-images fail as ECR does.
cat >"${tmp}/bin/aws" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"${AWS_LOG}"
if [[ "${IMAGE_MISSING:-0}" != 0 ]]; then
  echo "An error occurred (ImageNotFoundException) when calling the DescribeImages operation" >&2
  exit 254
fi
echo '{"imageDetails": []}'
EOF
chmod +x "${tmp}/bin/terraform" "${tmp}/bin/aws"
export PATH="${tmp}/bin:${PATH}" AWS_LOG="${tmp}/aws.log"

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
  : >"${AWS_LOG}"
  (cd "${tmp}" && "${script}" bootstrap) >"${tmp}/out" 2>"${tmp}/err"
}

before="${failures}"
if run_check; then
  expected="ecr describe-images --region eu-west-1 --repository-name slipstream/orchestration --image-ids imageTag=main"
  [[ "$(cat "${AWS_LOG}")" == "${expected}" ]] || fail "published: looked up '$(cat "${AWS_LOG}")'"
  [[ ! -s "${tmp}/out" ]] || fail "published: printed '$(cat "${tmp}/out")' to stdout"
else
  fail "published image: exited non-zero: $(cat "${tmp}/err")"
fi
report "${before}" "published image passes"

before="${failures}"
if IMAGE_MISSING=1 run_check; then
  fail "missing image: exited zero"
else
  grep -q "slipstream/orchestration:main is not published" "${tmp}/err" || fail "missing image: no clear message: $(cat "${tmp}/err")"
  grep -q "orchestration-image workflow" "${tmp}/err" || fail "missing image: does not name the publish step"
  grep -q "ImageNotFoundException" "${tmp}/err" || fail "missing image: hides the aws error"
fi
report "${before}" "missing image fails naming the publish step"

for output in region orchestration_image_repo_url; do
  before="${failures}"
  if EMPTY_OUTPUT="${output}" run_check; then
    fail "empty ${output}: exited zero"
  else
    grep -q "bootstrap output ${output} is empty" "${tmp}/err" || fail "empty ${output}: no clear message: $(cat "${tmp}/err")"
    [[ ! -s "${AWS_LOG}" ]] || fail "empty ${output}: called aws anyway"
  fi
  report "${before}" "empty ${output} aborts before the lookup"
done

[[ "${failures}" == 0 ]] || {
  echo "${failures} failure(s)" >&2
  exit 1
}
