# syntax=docker/dockerfile:1.7
# Orchestration image: the lean Prefect-worker image that drives the knob sweep
# (ADR-0012 §Amendment, ADR-0015 unattended-sweep). It carries the orchestration member
# — Prefect + boto3 + the sweep flow code — and NOTHING of the model: the worker
# issues SSM SendCommand to the bench host and reconfigures deploy/vllm-gpu, but never
# serves or tokenizes, so it shares no base with the bench-client image (bench/Dockerfile).
# The eks worker layer (terraform/eks/worker.tf) runs it under the prefect-worker Helm
# chart, which supplies the container command; the CMD here only makes a bare `docker run`
# do something sane.
#
# Two stages: a uv builder resolves the locked dependency set into a venv, and a Prefect
# runtime (prefecthq/prefect) carries that venv on top of the worker runtime contract the
# prefect-worker Helm chart assumes — tini as PID-1 (/usr/bin/tini), /opt/prefect/entrypoint.sh,
# and an arbitrary-uid writable HOME the chart's securityContext pins. Both base images are
# pinned by digest (a tag is a mutable pointer; a digest is the content) and Dependabot
# advances them.

# uv 0.9.30-python3.11-bookworm-slim
FROM ghcr.io/astral-sh/uv@sha256:4f5d923c9dcea037f57bda425dd209f3ec643da2f0b74227f68d09dab0b3bb36 AS builder

# Byte-compile on install so the worker doesn't pay first-import compilation, and copy
# packages into the venv rather than symlinking the uv cache, so the venv is self-contained
# when it is copied into the runtime stage that has no cache.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install the locked dependency set first, without any workspace member, so this layer — the
# slow one — is cached on the lockfile and pyproject alone and survives every source-only edit.
# --no-install-workspace skips the orchestration member and its slipstream-contract dependency,
# which have no source in this layer; --frozen fails if the lockfile is stale rather than
# silently re-resolving; --no-dev drops the dev group (pytest/ruff/ty); --package
# slipstream-orchestration selects the orchestration member, pulling Prefect + boto3. The
# cache mount persists uv's download cache across builds without landing in a layer, and
# the bind mounts read the manifests without copying them into this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-workspace --no-dev --package slipstream-orchestration

# Now the source, then install the orchestration member and its workspace dependency into the
# same venv. packages/ carries both the orchestration member and the slipstream-contract kernel
# it depends on via [tool.uv.sources]; --no-editable bakes them into site-packages so the runtime
# stage needs no source tree on disk. Only orchestration's own dependency tree is installed, so
# the image carries neither the executor nor the report member's plotting stack.
COPY pyproject.toml uv.lock ./
COPY packages ./packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package slipstream-orchestration

# prefecthq/prefect 3.8.7-python3.11 — the base carries the worker runtime contract the
# prefect-worker Helm chart's command and securityContext assume (tini as PID-1,
# /opt/prefect/entrypoint.sh, an arbitrary-uid writable HOME). A bare python:slim base
# lacks all three, so the chart's `tini -g -- /opt/prefect/entrypoint.sh` never starts.
FROM prefecthq/prefect@sha256:b3cdfebebff039d1c171947a61a0c37688c6336a1aa659841c3642265d0b2484

# The pinned digest freezes the base's apt package set, which drifts behind Debian security
# updates (openssl, kernel headers). Pull the fixed OS packages so the image clears the Trivy
# HIGH/CRITICAL scan-gate, cleaning the apt lists in the same layer so nothing lands in the image.
RUN apt-get update \
    && apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/*

# The worker drives the per-point GPU redeploy and ceiling scrape by shelling out to
# kubectl (slipstream_bench.orchestration.cluster), so the image must carry the binary.
# Pinned to the EKS control-plane minor (terraform/eks kubernetes_version 1.36) to hold
# the client within kubectl's +/-1 version-skew support. BuildKit's ADD --checksum
# verifies the download against the release sha256 (the pin-by-content rule the base
# image digests follow), failing the build on any mismatch; --chmod makes it executable
# for the arbitrary uid the prefect-worker chart runs as. amd64 to match the worker nodes.
ADD --chmod=0755 \
    --checksum=sha256:33bc88a24c3b09cf55bfd59e6ca977881b61fc3f25ab65bc80c65b430248648e \
    https://dl.k8s.io/release/v1.36.5/bin/linux/amd64/kubectl \
    /usr/local/bin/kubectl

# The venv built in the builder stage carries the orchestration member's deps and flow code.
# Prepend its bin so the locked `prefect` and the package's console scripts resolve ahead of the
# base image's own Prefect. The chart runs the container as an arbitrary uid; the venv is copied
# world-readable (0755 dirs, 0644 files), so that uid can read and execute it without a chown.
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

# The three deep-config inputs the registered knob-sweep deployment reads on the worker at run
# time (register-knob-sweep's in-image default paths, slipstream_bench.orchestration.cli): model.yaml
# and sweep-grid.yaml are the served model and the sweep's grid, vllm-gpu.yaml is the redeploy template
# whose serving image ref folds into the deep digest (ADR-0012). The worker has no source tree, so
# they are baked rather than fetched. Copied low and narrowly so a config edit rebuilds only this
# layer, never the dependency install above it. These files are image inputs: the content tag in
# orchestration-image.yml hashes them and the build triggers watch them, so an edit republishes a
# new image.
COPY model.yaml /app/model.yaml
COPY bench/sweep-grid.yaml /app/bench/sweep-grid.yaml
COPY k8s/vllm-gpu.yaml /app/k8s/vllm-gpu.yaml

# This image's own ECR ref (registry/repo:content-tag), passed by orchestration-image.yml at
# build time and exported so the knob-sweep flow running in this image can read which
# orchestration build drove a run. A process work pool gives the flow no runtime handle to the
# worker image, so the ref is baked here rather than discovered at run time; the content tag is
# the image's own idempotency key, so for a given image the baked value is deterministic. Empty
# on a bare `docker build` and the PR scan build, which pass no build arg. Placed after the
# dependency install so this per-commit value rebuilds only its own layer, not the install above.
ARG ORCH_IMAGE_REF=""
ENV ORCH_IMAGE_REF=${ORCH_IMAGE_REF}

# The prefect-worker Helm chart supplies the real command (tini -> entrypoint.sh -> prefect
# worker start …). This default only serves a bare `docker run` — a version print that fails
# loudly if the install is broken.
CMD ["prefect", "version"]
