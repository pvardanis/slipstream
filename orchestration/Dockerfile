# syntax=docker/dockerfile:1.7
# Orchestration image: the lean Prefect-worker image that drives the knob sweep
# (ADR-0012 §Amendment, ADR-0015 unattended-sweep). It carries the package's `.[orchestration]` extra
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

# Install the locked dependency set first, without the project, so this layer — the slow
# one — is cached on the lockfile and pyproject alone and survives every source-only edit.
# --frozen fails if the lockfile is stale rather than silently re-resolving; --no-dev drops
# the dev group (pytest/ruff/ty); --extra orchestration pulls Prefect + boto3. The cache
# mount persists uv's download cache across builds without landing in a layer, and the bind
# mounts read the manifests without copying them into this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --extra orchestration

# Now the source, then install the project itself into the same venv. --no-editable bakes
# the package into site-packages so the runtime stage needs no source tree on disk.
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra orchestration

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

# The venv built in the builder stage carries the .[orchestration] deps and the sweep flow code.
# Prepend its bin so the locked `prefect` and the package's console scripts resolve ahead of the
# base image's own Prefect. The chart runs the container as an arbitrary uid; the venv is copied
# world-readable (0755 dirs, 0644 files), so that uid can read and execute it without a chown.
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

# The prefect-worker Helm chart supplies the real command (tini -> entrypoint.sh -> prefect
# worker start …). This default only serves a bare `docker run` — a version print that fails
# loudly if the install is broken.
CMD ["prefect", "version"]
