#!/usr/bin/env bash
# Derives the bench-client image's identity from the model definition
# (model.yaml) and git history, so the justfile build/pull recipes and the CI
# workflow name the image identically. Prints one field, selected by $1:
#   hf-id     the served model's Hugging Face id (the tokenizer baked in)
#   revision  the served model's pinned Hugging Face revision
#   slug      the tokenizer slug: the model name, lowercased, non-alnum runs -> '-'
#   sha       short hash of the last commit touching the image's inputs
#   sha-tag   <slug>-<sha>, the immutable content tag
#   main-tag  <slug>-main, the floating pointer to the current main build
#
# MODEL_FILE overrides the model definition path (used by the test fixtures);
# it defaults to model.yaml at the repo root.
set -euo pipefail

if ! command -v yq >/dev/null 2>&1; then
  echo "image-tag.sh: yq not found; install mikefarah yq (brew install yq)" >&2
  exit 1
fi

field="${1:?usage: image-tag.sh <hf-id|revision|slug|sha|sha-tag|main-tag>}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
model_file="${MODEL_FILE:-${repo_root}/model.yaml}"

# Read a required scalar from the model definition. yq prints the literal `null`
# for an absent key rather than failing, so reject that here: a missing key must
# stop the build, not tag the image with a `null` slug.
read_key() {
  local key="$1" val
  val="$(yq -r "${key}" "${model_file}")"
  if [[ -z "${val}" || "${val}" == "null" ]]; then
    echo "image-tag.sh: ${model_file} is missing ${key}" >&2
    exit 1
  fi
  printf '%s' "${val}"
}

hf_id="$(read_key '.model.hfId')"
revision="$(read_key '.model.revision')"

# The slug names the tokenizer the image carries: the model name (the id's final
# path segment, dropping the hosting org) lowercased, with every run of
# non-alphanumeric chars collapsed to one '-'. Qwen/Qwen3-8B-AWQ -> qwen3-8b-awq.
slug="$(printf '%s' "${hf_id##*/}" |
  tr '[:upper:]' '[:lower:]' |
  sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"

# An hfId whose final path segment holds no alphanumerics collapses to an empty
# slug, which would tag the image `-main` / `-<sha>`. Reject it: a tag with no
# model name is meaningless, so stop the build rather than push it.
if [[ -z "${slug}" ]]; then
  echo "image-tag.sh: model hfId '${hf_id}' yields an empty slug" >&2
  exit 1
fi

# The short hash of the last commit touching any image input. A change to any of
# these is what a new content tag must capture. This names committed state only:
# uncommitted edits to the inputs are not reflected, since the image is built from
# the committed tree (in CI, from the pushed commit). Empty means nothing is
# committed yet — a content tag would be a lie, so fail loudly rather than tag `-`.
image_sha() {
  local sha
  sha="$(git -C "${repo_root}" log -1 --format=%h -- \
    bench src pyproject.toml model.yaml)"
  if [[ -z "${sha}" ]]; then
    echo "image-tag.sh: no committed change touches the image inputs;" \
      "cannot derive a content sha" >&2
    exit 1
  fi
  printf '%s' "${sha}"
}

case "${field}" in
hf-id) printf '%s\n' "${hf_id}" ;;
revision) printf '%s\n' "${revision}" ;;
slug) printf '%s\n' "${slug}" ;;
main-tag) printf '%s-main\n' "${slug}" ;;
# Assign before printing: a failing command substitution inside a printf
# argument does not trip `set -e` (printf still succeeds), so image_sha's
# `exit 1` would be swallowed and a `-`-suffixed tag printed. A bare assignment
# does honour `set -e`, so capture the sha first, then print.
sha)
  sha="$(image_sha)"
  printf '%s\n' "${sha}"
  ;;
sha-tag)
  sha="$(image_sha)"
  printf '%s-%s\n' "${slug}" "${sha}"
  ;;
*)
  echo "image-tag.sh: unknown field '${field}'" >&2
  exit 1
  ;;
esac
