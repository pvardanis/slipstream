<!-- Glossary for the slipstream inference platform. Ubiquitous language only — terms and what they mean in this domain. No implementation details, no spec, no decisions (those live in the wayfinder map and docs/adr/). -->

# slipstream — Context glossary

The domain is **LLM inference serving on AWS EKS**: cluster-level orchestration *around*
the inference server. Terms below are the shared vocabulary; use them exactly.

## Platform

- **Rig** — the locked starting hardware + model + quantization the platform is built and
  measured against: A10G 24GB (`g5.xlarge` on-demand) running Qwen3-8B, AWQ-INT4 weights with an
  FP8 KV cache.
- **Layer (L0–L4)** — a depth-first band of the platform. L0 benchmark harness · L1 single
  tuned replica · L2 platform (autoscaling, cold start, spot handling — the gap) · L3
  inference-aware routing · L4 prefill/decode disaggregation.
- **Duty-cycle** — the platform does not run 24/7; the GPU budget buys a fraction of the day.
  `just cluster-up`/`just cluster-down` fully create and destroy GPU capacity, so idle time costs nothing.

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
- **Engine knob** — a serving-engine argument that trades KV-cache capacity, latency, or
  throughput: `max-num-seqs`, `kv-cache-dtype`, chunked prefill, prefix caching. What the L1
  knob sweep varies to find the ceiling; distinct from client-side workload (arrival rate,
  prefix share).
- **Goodput** — throughput counting only requests that meet the SLO (both `ttft` and `tpot`
  thresholds). The metric that matters: raw throughput past the SLO cliff is requests served
  too slowly to count.
- **Concurrency ceiling** — the highest offered concurrency (simultaneous in-flight requests)
  at which a replica still holds goodput ≥ its SLO fraction. The single measured number the L1
  knob sweep produces per engine-knob configuration.

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

## Bench

- **Model definition** — the single record of the served model (HF id, HF revision, quantization,
  KV-cache dtype) that both the bench-client image's tokenizer and the serving Engine derive from.
  Client and server must tokenize identically, so they read one definition, not two copies.
- **Bench-client image** — the container running the benchmark client, baking the served model's
  tokenizer at build time so its token counts match the Engine's.
- **Tokenizer slug** — the served-model identifier carried in the bench-client image's tag, so a
  tag names the tokenizer the image contains rather than an opaque `latest`.
- **Knob sweep** — the whole L1 campaign: the measurement that varies engine knobs across a
  grid to find the concurrency ceiling at the goodput SLO. One knob sweep is many point
  sweeps — one per engine point — each preceded by its own GPU redeploy.
- **Point sweep** — one engine point's measurement: the client-load ladder run against a
  single running replica (one GPU deployment), producing that point's concurrency ceiling.
  The knob sweep is the parent over every point sweep.
- **Ladder** — the client concurrencies a measurement steps through against one running
  replica, one cell per step.
- **Rung** — one step of a ladder: a single cell at one offered concurrency.

## KV cache A/B

- **KV cache A/B** — the measurement of KV cache offload off against on at one engine point
  (`kv-ab` for short; its Prefect flow is `kv-cache-extension-ab`, offload extending the KV
  cache past GPU memory).
- **KV cache offload** — the engine copying KV cache blocks out of GPU memory into a
  CPU-memory tier (LMCache), so a prefix evicted from the GPU can be loaded back instead of
  recomputed.
- **KV cache extension** — the reuse KV cache offload adds past the GPU KV pool: prefixes
  the GPU has evicted that a request loads back from the CPU tier instead of recomputing.
  The A/B measures it. There is none unless the CPU tier holds more than the GPU KV pool,
  because the tier copies every prefill and both evict least recently used first.
- **Chunk** — the unit the CPU tier stores and reuses KV cache in: a fixed run of tokens,
  configured as a whole number of the engine's blocks. A prefix reuses only its whole chunks.
- **Working set** — the distinct prefix tokens a workload keeps reusing. Offload can only
  help once the working set is larger than the GPU KV pool.
- **Arm** — one side of the A/B: KV cache offload **off** or **on**. Everything else — engine
  knobs, workload, ladder — is held the same across both.
- **Run** — one repetition of an arm: a fresh redeploy, one unrecorded warmup cell, then
  the full ladder. Each arm gets the same number of runs, numbered from 1. Not the run id,
  which names the whole A/B (as it names a whole knob sweep) and every run in it.
- **Arm run** — a run named by its arm and number, `<arm>-r<n>` (e.g. `on-r2`). The A/B
  interleaves its arm runs off, on, off, on, and nests them all under one run id.
- **Prefix-cache window** — the advance of the engine's prefix-cache counters over one
  cell, read as the difference between a snapshot before and after it. Its GPU hit rate
  counts tokens the GPU prefix cache served; its combined hit rate adds the tokens the CPU
  tier served.
- **Spread** — how far one rung's metric varies across an arm's runs, reported as the median
  and the range.

## Observability

- **Request-ID** — one identifier carried across Grafana (fleet health), Sentry (code-path
  errors), and PostHog (product) via OpenTelemetry, so a single request is traceable end to end.
