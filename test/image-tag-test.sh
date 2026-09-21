#!/usr/bin/env bash
# Unit test for bench/image-tag.sh: the model-definition file and git history are
# the script's only inputs, so the served model id/revision, the tokenizer slug,
# and the two content tags are asserted against known model.yaml fixtures. No
# cluster, no ECR — pure derivation.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/bench/image-tag.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

fail=0
check() {
  local label="$1" want="$2" got="$3"
  if [[ "${got}" != "${want}" ]]; then
    echo "FAIL ${label}: want '${want}', got '${got}'" >&2
    fail=1
  else
    echo "ok   ${label}"
  fi
}

# A model id with an org prefix and mixed case: the slug drops the org and
# lowercases, so the tag names the tokenizer, not the account that hosts it.
cat >"${tmp}/model.yaml" <<'YAML'
model:
  hfId: Qwen/Qwen3-8B-AWQ
  revision: 4da05a8edb55c6046cce958586c33b61da07bb79
  quantization: awq_marlin
  kvCacheDtype: fp8
YAML

check "hf-id" "Qwen/Qwen3-8B-AWQ" \
  "$(MODEL_FILE="${tmp}/model.yaml" "${script}" hf-id)"
check "revision" "4da05a8edb55c6046cce958586c33b61da07bb79" \
  "$(MODEL_FILE="${tmp}/model.yaml" "${script}" revision)"
check "slug" "qwen3-8b-awq" \
  "$(MODEL_FILE="${tmp}/model.yaml" "${script}" slug)"
check "main-tag" "qwen3-8b-awq-main" \
  "$(MODEL_FILE="${tmp}/model.yaml" "${script}" main-tag)"

# A bare id with a dotted, mixed-case name: every run of non-alphanumeric chars
# collapses to a single '-'.
cat >"${tmp}/dotted.yaml" <<'YAML'
model:
  hfId: Meta-Llama/Llama-3.1-8B-Instruct
  revision: main
YAML
check "slug dotted" "llama-3-1-8b-instruct" \
  "$(MODEL_FILE="${tmp}/dotted.yaml" "${script}" slug)"

# sha-tag is <slug>-<short-sha>; the sha is the short hash of the last commit
# touching the image inputs, so assert its shape against the repo's real
# model.yaml rather than a fixed value that would churn every rebuild.
sha_tag="$("${script}" sha-tag)"
if [[ "${sha_tag}" =~ ^qwen3-8b-awq-[0-9a-f]{7,}$ ]]; then
  echo "ok   sha-tag shape (${sha_tag})"
else
  echo "FAIL sha-tag shape: got '${sha_tag}'" >&2
  fail=1
fi

# An unknown field is a caller bug, not an empty string the build would tag with.
if "${script}" bogus-field >/dev/null 2>&1; then
  echo "FAIL unknown field: expected non-zero exit" >&2
  fail=1
else
  echo "ok   unknown field rejected"
fi

# Assert a field lookup exits non-zero: a missing key or garbage slug must stop
# the build, not silently tag the image with `null` or an empty slug.
expect_fail() {
  local label="$1" model="$2" field="$3"
  if MODEL_FILE="${model}" "${script}" "${field}" >/dev/null 2>&1; then
    echo "FAIL ${label}: expected non-zero exit" >&2
    fail=1
  else
    echo "ok   ${label}"
  fi
}

# An absent hfId: yq returns the literal `null`, which must be rejected rather
# than baked into a `null`-slugged tag.
cat >"${tmp}/no-hfid.yaml" <<'YAML'
model:
  revision: main
YAML
expect_fail "missing hfId rejected" "${tmp}/no-hfid.yaml" slug

# An hfId whose name segment holds no alphanumerics collapses to an empty slug.
cat >"${tmp}/empty-slug.yaml" <<'YAML'
model:
  hfId: org/___
  revision: main
YAML
expect_fail "empty slug rejected" "${tmp}/empty-slug.yaml" main-tag

exit "${fail}"
