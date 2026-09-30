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
# Two stages: a uv builder resolves the locked dependency set into a venv, and a slim
# runtime carries only that venv — no uv, no compiler, no lockfile. Both base images are
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

FROM python:3.11-slim@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b

# Run as a non-root user: the worker holds AWS access through its Pod Identity
# association (S3 + SSM), so a container escape must not land as root. The venv and the
# Prefect home the worker writes to are owned by this user.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --create-home --home-dir /home/app app

# The venv built in the previous stage is the whole runtime: no uv, no compiler, no
# lockfile, no source tree. Put its bin on PATH so `prefect` and the package's console
# scripts resolve without activation.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

# Pin Prefect's home to the app user's writable home so profile/state writes land
# somewhere owned, not a root-owned default the non-root worker cannot create.
ENV PREFECT_HOME=/home/app/.prefect

USER app
WORKDIR /home/app

# The prefect-worker Helm chart supplies the real command (prefect worker start …). This
# default only serves a bare `docker run` — a version print that fails loudly if the
# install is broken.
CMD ["prefect", "version"]
