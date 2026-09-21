#!/usr/bin/env bash
# Builds the bench-client image from the model definition, the one build command
# the justfile recipe and both CI workflows share. It derives the served model's
# id and revision (via image-tag.sh) into the Dockerfile's build args and builds
# for the amd64 nodes. Each argument is an image ref to tag the build with (zero
# or more): the PR check passes none, the local recipe the -sha tag, the publish
# workflow both the -sha and -main tags. It neither logs in nor pushes — the
# caller owns the registry.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${here}/.." && pwd)"
image_tag="${here}/image-tag.sh"

# Assign before use: a failing command substitution inside a --build-arg does not
# trip `set -e` (docker build still runs), so an image-tag.sh error would build
# with an empty MODEL/REVISION. A bare assignment does honour `set -e`.
model="$("${image_tag}" hf-id)"
revision="$("${image_tag}" revision)"

# One -t flag per image ref. Kept in an array so no refs means no -t flag at all;
# the ${arr[@]+…} guard keeps that empty case safe under `set -u` on bash 3.2.
tag_flags=()
for ref in "$@"; do
  tag_flags+=(-t "${ref}")
done

docker build --platform linux/amd64 \
  --build-arg MODEL="${model}" --build-arg REVISION="${revision}" \
  ${tag_flags[@]+"${tag_flags[@]}"} \
  -f "${repo_root}/bench/Dockerfile" "${repo_root}"
