# Serving lifecycle: the vLLM api-key Secret, CPU and GPU replicas, their scale and completion smokes, and the GPU manifest render.

# Ensure the vLLM api-key Secret exists. Generated once and left stable across
# deploys: vLLM enforces it on its API routes, and the bench endpoint stack reads the
# same value into Secrets Manager for the bench client. Regenerating it would
# lock out an api-key already published to a running bench endpoint.
_ensure-api-key:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl create namespace slipstream --dry-run=client -o yaml | kubectl apply -f -
    # `--ignore-not-found` prints nothing when the Secret is absent but still errors
    # on a real failure (unreachable API server, wrong context, RBAC), so a
    # connection problem can't masquerade as "absent" and mint a fresh key against
    # the wrong cluster — diverging from a key already published to a bench endpoint.
    if [[ -z "$(kubectl -n slipstream get secret vllm-api-key --ignore-not-found -o name)" ]]; then
      # Bare assignment so a failed openssl aborts: in argument position a failed
      # $(...) does not trip set -e, which would create a Secret with an empty key.
      key="$(openssl rand -hex 32)"
      [[ -n "${key}" ]] || { echo "openssl produced no api-key" >&2; exit 1; }
      kubectl -n slipstream create secret generic vllm-api-key --from-literal=api-key="${key}"
    fi

# Read the vLLM api-key out of the cluster Secret, failing loudly if the Secret
# exists but carries no api-key value rather than emitting an empty credential
# (an empty key would then authenticate nothing and be published to a bench endpoint).
_read-api-key:
    #!/usr/bin/env bash
    set -euo pipefail
    key="$(kubectl -n slipstream get secret vllm-api-key -o jsonpath='{.data.api-key}' | base64 -d)"
    [[ -n "${key}" ]] || { echo "vllm-api-key Secret has no api-key value" >&2; exit 1; }
    printf '%s' "${key}"

# Deploy the CPU vLLM replica and wait for it to serve.
cpu-deploy: _ensure-api-key
    kubectl apply -f {{ manifests }}
    kubectl -n slipstream rollout status deploy/vllm --timeout=600s

# Remove the vLLM workload (leaves the cluster running).
undeploy:
    kubectl delete -f {{ manifests }} --ignore-not-found

# Render the GPU manifest's swept engine knobs from the environment, defaulting to
# the committed rig config (max-num-seqs 16, FP8 KV, prefix caching on) so a plain
# `just gpu-deploy` deploys the committed rig config. `just knob-sweep` sets
# MAX_NUM_SEQS / KV_CACHE_DTYPE / PREFIX_CACHING_FLAG per sweep point (#33, ADR-0009).
# Only these three placeholders are substituted, so nothing else in the manifest
# (an image tag, a shell-like token) is touched. Prints to stdout — pipe to
# `kubectl apply -f -` (or `kubectl diff -f -` to preview before applying).
_render-gpu-manifest:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v envsubst >/dev/null || { echo "envsubst required (brew install gettext)" >&2; exit 1; }
    MAX_NUM_SEQS="${MAX_NUM_SEQS:-16}" \
    KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}" \
    PREFIX_CACHING_FLAG="${PREFIX_CACHING_FLAG:---enable-prefix-caching}" \
      envsubst '${MAX_NUM_SEQS} ${KV_CACHE_DTYPE} ${PREFIX_CACHING_FLAG}' <{{ gpu_manifests }}

# Deploy the GPU vLLM replica and wait for it to serve. Karpenter provisions the
# g5 on demand once the pod requests a GPU, so the wait covers node bring-up, the
# ~10 GB image pull, and the AWQ weight load — hence the long timeout. Reuses the
# vllm-api-key Secret (_ensure-api-key), the same key the CPU replica uses. The
# manifest is rendered (see `_render-gpu-manifest`) so the swept engine knobs
# default to the committed rig unless the knob sweep overrides them.
gpu-deploy: _ensure-api-key
    #!/usr/bin/env bash
    set -euo pipefail
    just _render-gpu-manifest | kubectl apply -f -
    kubectl -n slipstream rollout status deploy/vllm-gpu --timeout=1200s

# Scale the GPU replica up to one and wait for it to serve. Karpenter brings a g5
# node up to place it.
gpu-up:
    kubectl -n slipstream scale deploy/vllm-gpu --replicas=1
    kubectl -n slipstream rollout status deploy/vllm-gpu --timeout=1200s

# Scale the GPU replica to zero. The g5 node goes empty and Karpenter reaps it
# after its consolidation window, returning GPU spend to zero (ADR-0006).
gpu-down:
    kubectl -n slipstream scale deploy/vllm-gpu --replicas=0

# Port-forward the service and curl a completion out of it.
cpu-completion:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl -n slipstream rollout status deploy/vllm --timeout=600s
    kubectl -n slipstream port-forward svc/vllm 8000:8000 >/dev/null 2>&1 &
    pf_pid=$!
    trap 'kill "${pf_pid}" 2>/dev/null || true' EXIT
    for _ in $(seq 30); do
      curl -sf http://localhost:8000/health >/dev/null 2>&1 && break
      sleep 1
    done
    # vLLM enforces the api-key on its API routes; read it from the Secret.
    key="$(just _read-api-key)"
    curl -sf http://localhost:8000/v1/completions \
      -H 'Content-Type: application/json' \
      -H "Authorization: Bearer ${key}" \
      -d '{"model":"{{ cpu_model }}","prompt":"The slipstream platform serves","max_tokens":32}'
    echo

# Smoke the GPU replica: assert HTTP 200 and a well-formed, non-empty completion
# from the live engine (ADR-0002 item 5, for the GPU path). Proves the deploy →
# service → engine → token path end-to-end; it does not judge answer quality
# (out of scope, spec §5). This is the "harness green" step of `just cloud-verify`.
gpu-completion:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl -n slipstream rollout status deploy/vllm-gpu --timeout=1200s
    kubectl -n slipstream port-forward svc/vllm-gpu 8000:8000 >/dev/null 2>&1 &
    pf_pid=$!
    trap 'kill "${pf_pid}" 2>/dev/null || true' EXIT
    ready=""
    for _ in $(seq 60); do
      curl -sf http://localhost:8000/health >/dev/null 2>&1 && { ready=1; break; }
      sleep 1
    done
    [[ -n "${ready}" ]] || { echo "vllm-gpu /health never came up" >&2; exit 1; }
    # vLLM enforces the api-key on its API routes; read it from the Secret.
    key="$(just _read-api-key)"
    # The request's model field must name what the GPU replica serves; read it from
    # model.yaml, the source of truth the manifest is held to.
    model="$({{ image_tag_tool }} hf-id)"
    body="$(mktemp)"
    trap 'kill "${pf_pid}" 2>/dev/null || true; rm -f "${body}"' EXIT
    code="$(curl -s -o "${body}" -w '%{http_code}' http://localhost:8000/v1/completions \
      -H 'Content-Type: application/json' \
      -H "Authorization: Bearer ${key}" \
      -d "{\"model\":\"${model}\",\"prompt\":\"The slipstream platform serves\",\"max_tokens\":32}")"
    [[ "${code}" == "200" ]] || { echo "completion returned HTTP ${code}:" >&2; cat "${body}" >&2; exit 1; }
    # Assert the body carries a non-empty completion — shape and liveness, not
    # answer quality. A 200 with an empty choices/text still means the engine
    # served nothing, so it must fail here.
    uv run python -c 'import json,sys; d=json.load(open(sys.argv[1])); c=d.get("choices") or []; t=(c[0].get("text","") if c else ""); sys.exit(0 if t.strip() else "completion had no non-empty text: "+json.dumps(d))' "${body}"
    echo "gpu completion ok"
