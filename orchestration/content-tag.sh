#!/usr/bin/env bash
# Prints the orchestration image's immutable content tag: the first 12 hex characters of
# the hash of the last commit that touched any image input. The publish workflow
# (.github/workflows/orchestration-image.yml) tags the image with it, and Image Updater
# rolls the worker only to tags matching its ^[0-9a-f]{12}$ allow-tags pattern
# (k8s/argocd/apps/templates/prefect-worker.yaml); test/argocd-apps-test.sh fails when
# this output stops matching that pattern.
#
# The inputs are the package source, the whole packages/ tree (the
# slipstream-bench-contract workspace member it installs; only installed members belong
# there, so the tree needs no per-member list), its locked dependency set, the build
# definition, the deep-config files the image bakes for the worker (model.yaml, the sweep
# grid, the kv-ab workload, the vLLM redeploy manifest), and the bench-endpoint stack for
# the worker to apply with the Terraform CLI configuration that installs its providers
# from the image; the stack's tests are not baked. A change to any of these is what a new
# content tag must capture; a change elsewhere (docs, the other terraform stacks, the
# bench-endpoint stack's tests) leaves the image identical, so the workflow finds the tag
# present and skips the rebuild. Markdown is excluded: CI reads a markdown-only change as
# docs-only (ADR-0027), so it must not name an image nothing builds. The PR build gate in
# .github/workflows/ci.yml (the changes job's orchestration_image filter) mirrors this
# pathspec; test/ci-paths-parity-test.sh fails when the two differ.
#
# The tag is cut from the full hash rather than taken from git's short hash, which is
# only a minimum width: git lengthens it when 12 characters are ambiguous. A fixed cut
# keeps the tag for a commit constant, so the workflow's presence probe finds it. Needs
# full history: in a shallow clone every input looks last changed at the graft.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Assigned on its own line so a failing git stops the script under set -e.
sha="$(git -C "${repo_root}" log -1 --format=%H -- \
  packages pyproject.toml uv.lock orchestration/Dockerfile .dockerignore \
  model.yaml bench/configs/sweep-grid.yaml bench/configs/kv-cache-extension-ab.yaml \
  k8s/vllm-gpu.yaml orchestration/provider-mirror.tfrc terraform/bench-endpoint \
  ':(exclude)terraform/bench-endpoint/tests' ':(exclude,glob)**/*.md')"
if [[ -z "${sha}" ]]; then
  echo "content-tag.sh: no committed change touches the orchestration image inputs; cannot derive a content tag" >&2
  exit 1
fi
printf '%s\n' "${sha:0:12}"
