# syntax=docker/dockerfile:1.7
# Bench-client image: the slipstream-bench harness and the model tokenizer, baked
# onto the same pinned vLLM engine build the server runs (k8s/vllm.yaml). Building
# on that base means the client's transformers/tokenizers versions match the
# server's exactly, so the two tokenize identically, and it keeps the `vllm bench
# serve` CLI the harness drives. The container runs one cell per `docker run` —
# `load-cell` fires a single `vllm bench serve` (the grid loop lives in the
# orchestration layer, ADR-0012 §Amendment). Launched in-cluster, the client
# reaches vLLM over ClusterIP. Keep the base tag in sync with k8s/vllm.yaml.
FROM vllm/vllm-openai-cpu:v0.29.0-x86_64

# The model whose tokenizer is baked in, and the Hugging Face revision to bake it
# at. Sourced from model.yaml by `just bench-image` (the single source of truth
# for the served model); the defaults keep a bare `docker build` buildable.
ARG MODEL=Qwen/Qwen2.5-0.5B-Instruct
ARG REVISION=main

# Pin the Hugging Face cache to an explicit path so the baked tokenizer is found
# regardless of which user runs the client.
ENV HF_HOME=/opt/hf

# Layer the bench member onto the base image's interpreter, so the slipstream-bench
# console script lands alongside the vllm CLI on the same Python that provides
# transformers. The install is uv's, not pip's: the bench member depends on the
# slipstream-contract workspace member, which resolves only through the root's
# [tool.uv.sources] (pip cannot see a workspace source, and contract is
# unpublished); installing ./packages/bench rather than the root member keeps the
# plotting stack (pandas/seaborn) and Prefect out of the image, which carry the
# report and orchestration members that do not run here (ADR-0017). --system
# installs into the base image's interpreter additively, leaving its vllm and
# transformers in place, and --no-cache keeps uv's download cache out of the layer.
# The base's interpreter is a uv-managed CPython carrying a PEP-668
# EXTERNALLY-MANAGED marker, so --break-system-packages is required to install into
# it — the whole point here is to mutate that interpreter, the one the vllm CLI and
# the tokenizer bake below run on. The uv binary is copied from its own image,
# pinned by digest like the runtime bases; Dependabot advances it.
# uv 0.9.30
COPY --from=ghcr.io/astral-sh/uv@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 /uv /bin/uv
WORKDIR /opt/slipstream-bench
COPY pyproject.toml ./
COPY packages ./packages
RUN uv pip install --system --break-system-packages --no-cache ./packages/bench

# The experiment config is not baked: knob-sweep and prefix-cache compose a per-point
# config on the operator's laptop and mount it in as the harness's --config, so the
# image carries the tokenizer and harness only and stays identical across config
# edits. A bare `load-cell`/`load-sweep` with no --config exits 2 on the missing
# default, which no deployed path hits — every invocation passes --config.

# Bake the tokenizer at build time, pinned to the model's revision so the baked
# tokenizer matches the weights the server loads, and save it to a fixed path. The
# client, `vllm bench serve`, has no revision flag: it resolves a tokenizer only by
# name or local path. Saving to a stable directory lets the cell point --tokenizer at
# that path and load the pinned snapshot offline, with no ref to resolve. This fetch
# is the last step that needs the Hub.
RUN python3 -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('${MODEL}', revision='${REVISION}').save_pretrained('/opt/hf/tokenizer')"

# Name where the baked tokenizer lives so the image alone owns the path: `load-cell`
# reads this into its --tokenizer, and the host runner passes no path of its own.
ENV SLIPSTREAM_BENCH_TOKENIZER_DIR=/opt/hf/tokenizer

# Forbid any run-time Hub call, so the client tokenizes purely from the baked
# snapshot — no ref lookup, no soft failure on an egress-restricted pull. Set
# after the bake, which needs the Hub.
ENV HF_HUB_OFFLINE=1

# The container runs as the base image's root user, matching the server pod
# (k8s/vllm.yaml) and the throwaway nature of this harness; it holds no secrets
# and is torn down after each sweep, so a dedicated non-root user earns nothing.

# A manual harness pod: the orchestrating recipe execs the tools; keep the
# container alive so it can.
CMD ["sleep", "infinity"]
