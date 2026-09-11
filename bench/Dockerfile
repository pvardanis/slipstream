# syntax=docker/dockerfile:1.7
# Bench-client image: the slipstream-bench harness and the model tokenizer, baked
# onto the same pinned vLLM engine build the server runs (k8s/vllm.yaml). Building
# on that base means the client's transformers/tokenizers versions match the
# server's exactly, so the two tokenize identically, and it keeps the `vllm bench
# serve` CLI that serve-sweep drives. Launched in-cluster, the client reaches vLLM
# over ClusterIP. Keep the base tag in sync with k8s/vllm.yaml.
FROM vllm/vllm-openai-cpu:v0.29.0-x86_64

# The model whose tokenizer is baked in; matches k8s/vllm.yaml and the justfile.
ARG MODEL=Qwen/Qwen2.5-0.5B-Instruct

# Pin the Hugging Face cache to an explicit path so the baked tokenizer is found
# regardless of which user runs the client.
ENV HF_HOME=/opt/hf

# Layer the package onto the base image's interpreter with its own pip, so the
# slipstream-bench console script lands alongside the vllm CLI on the same Python
# that provides transformers. uv governs the package's dev workflow and lockfile;
# the image needs only the built wheel and its lone runtime dep (typer).
WORKDIR /opt/slipstream-bench
COPY pyproject.toml ./
COPY src ./src
RUN python3 -m pip install --no-cache-dir .

# Bake the tokenizer into HF_HOME at build time, against the version the base
# image pins. This fetch is the last step that needs the Hub.
RUN python3 -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('${MODEL}')"

# Forbid any run-time Hub call, so the client tokenizes purely from the baked
# cache — no revision check, no soft failure on an egress-restricted pull. Set
# after the bake, which needs the Hub.
ENV HF_HUB_OFFLINE=1

# The container runs as the base image's root user, matching the server pod
# (k8s/vllm.yaml) and the throwaway nature of this harness; it holds no secrets
# and is torn down after each sweep, so a dedicated non-root user earns nothing.

# A manual harness pod: the orchestrating recipe execs the tools; keep the
# container alive so it can.
CMD ["sleep", "infinity"]
