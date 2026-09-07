<!-- Glossary for the slipstream inference platform. Ubiquitous language only — terms and what they mean in this domain. No implementation details, no spec, no decisions (those live in the wayfinder map and docs/adr/). -->

# slipstream — Context glossary

The domain is **LLM inference serving on AWS EKS**: cluster-level orchestration *around*
the inference server. Terms below are the shared vocabulary; use them exactly.

## Platform

- **Rig** — the locked starting hardware + model + quantization the platform is built and
  measured against: A10G 24GB (`g5.xlarge` spot) running Qwen3-8B, AWQ-INT4 weights with an
  FP8 KV cache.
- **Layer (L0–L4)** — a depth-first band of the platform. L0 benchmark harness · L1 single
  tuned replica · L2 platform (autoscaling, cold start, spot handling — the gap) · L3
  inference-aware routing · L4 prefill/decode disaggregation.
- **Duty-cycle** — the platform does not run 24/7; the GPU budget buys a fraction of the day.
  `make up`/`make down` fully create and destroy GPU capacity, so idle time costs nothing.

## Serving

- **Engine** — the process that actually runs the model inside a pod (vLLM). Owns batching,
  the KV cache, quantization, and prefix caching. The orchestration layers drive it; they do
  not replace it.
- **Replica** — one running Engine instance serving the model. Concurrency happens *inside* a
  replica; routing happens *between* replicas.
- **Continuous batching** — a replica interleaving many in-flight requests on one GPU, adding
  and retiring them token by token.
- **KV cache** — the per-request key/value tensors the model computes during prefill and reuses
  during decode. Its size per sequence sets how many requests a replica can hold at once.
- **Prefill / Decode** — prefill reads the whole prompt and builds its KV cache; decode
  generates output tokens one at a time. Disaggregation (L4) scales them independently.
- **Prefix cache** — a replica retaining the KV cache of a previously-seen prompt prefix, so a
  later request sharing that prefix skips recomputing it.
- **Concurrency headroom** — the number of concurrent sequences a replica can hold after
  weights, i.e. the free KV-cache slots. The thing L1–L4 exist to fill, route, and scale.

## Routing (L3)

- **Router / Endpoint Picker (EPP)** — the component that chooses *which replica* a request
  goes to, using inference signals (prefix-cache locality, queue depth, KV utilization) rather
  than round-robin.
- **InferencePool** — the Gateway API Inference Extension CRD grouping the replicas the Router
  picks among.
- **Prefix-cache-aware routing** — routing a request to the replica most likely to already hold
  its prompt's prefix in KV cache. Approximate (in-memory index) or precise (replicas publish
  KV-cache events).
- **Multi-LoRA routing** — many LoRA adapters over one shared base model; routing an adapter-X
  request to a replica that already has adapter X loaded.

## Observability

- **Request-ID** — one identifier carried across Grafana (fleet health), Sentry (code-path
  errors), and PostHog (product) via OpenTelemetry, so a single request is traceable end to end.
