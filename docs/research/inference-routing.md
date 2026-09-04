<!-- Research notes: inference-aware routing for Layer 3 (prefix-cache-aware routing + multi-LoRA) on vLLM/EKS. Facts current to 2026-09-04. -->

# Inference-Aware Routing: Gateway API Inference Extension vs llm-d

## Recommendation

**Adopt the Gateway API Inference Extension (IGW / GIE) as the routing contract, and deploy the
llm-d Router (Endpoint Picker, "EPP") as the scheduler behind it.** These are not competing
choices — as of 2026 they are two layers of the same stack:

- IGW defines the Kubernetes API surface (the `InferencePool` CRD + the Endpoint Picker
  Protocol / ext-proc contract). It is the GA, vendor-neutral standard, and works with multiple
  gateways (Istio, kgateway, agentgateway, NGINX Gateway Fabric).
- llm-d supplies the actual prefix-cache-aware, load-aware scheduling logic. As of the IGW
  v1.6.0 release, the full-featured Endpoint Picker, body-based routing, and latency-predictor
  components were **moved out of the `kubernetes-sigs/gateway-api-inference-extension` repo into
  the `llm-d/llm-d-router` repo**. The upstream repo now ships only a lightweight reference EPP
  for conformance testing.

For slipstream's Layer 3 goals (beat round-robin on shared-prefix workloads + serve multiple
LoRA adapters on one base model), this pairing gives us: precise prefix-cache routing, KV-cache /
queue-depth / prefix-affinity scoring, and cache-aware LoRA routing — all against unmodified vLLM
on EKS.

**Caveat / assumption to flag:** IGW is GA, but llm-d itself is still pre-1.0 (v0.8/v0.9 series
as of 2026) and some IGW sub-APIs (`InferenceObjective`, adapter-rollout pipeline) are still
alpha/roadmap. The precise-prefix and LoRA-routing paths are usable today but should be treated
as fast-moving. Assuming we are comfortable tracking a pre-1.0 scheduler pinned to specific
versions — flag if we need a fully GA-frozen stack, in which case only the IGW contract + the
lightweight/approximate EPP path is "stable."

---

## Comparison

### 1. Maturity / release status

| | Gateway API Inference Extension (IGW/GIE) | llm-d |
|---|---|---|
| Governance | Kubernetes SIG (`kubernetes-sigs`), vendor-neutral | Community project: IBM, Google, Red Hat + others |
| Status | **GA** ("This project is GA'd" per README) | **Pre-1.0.** Headline releases in the v0.x series (v0.8 current headline, v0.9.0 the latest GitHub release observed) |
| Recent versions | v1.4.0 (Mar 2025), v1.5.0 (Apr 2025), **v1.6.0 (Aug 17, 2025)** latest observed | v0.2 → v0.9.x. LoRA + active-active HA landed in **v0.5** |
| API stability | `InferencePool` CRD is stable and stays in the SIG repo. `InferenceObjective` / `InferenceModelRewrite` remain **alpha** (expect breaking changes) | Well-lit paths are "tested recipes" but the project is explicitly pre-GA |

Notes / uncertainty:
- Sources disagree slightly on the "latest" IGW version (one reference deployment pins v1.5.0
  with Gateway API v1.6.0; the releases page shows v1.6.0 as latest). Treat exact pins as
  version-of-the-day and confirm against the releases page at deploy time.
- The *Inference Gateway* component reached a v1.0 GA milestone (delivered around llm-d v0.3).
  The **llm-d project as a whole has NOT shipped a v1.0** — do not claim otherwise.
- Some secondary sources date llm-d v0.5 to "February 2026." I could not confirm the exact date
  from a primary source; treat the date as unverified.

### 2. Routing signals exposed

Both use the same Endpoint Picker Protocol; the scheduler consumes vLLM telemetry (Prometheus
metrics) plus, in precise mode, a live KV-cache event stream.

**Scoring pipeline (llm-d Router / EPP):** `Filter → Score → Pick`. Scorers are pluggable and
weighted. Default and available scorers:

- **Prefix-cache affinity** — scores a pod by how much of the request's prompt prefix is
  believed resident in that pod's KV cache. Weighted highest by default.
- **KV-cache utilization** — maps to vLLM's `kv_cache_usage`.
- **Queue depth** — maps to vLLM's `num_requests_waiting`; the queue-scorer / load-aware-scorer
  penalizes pods with longer queues.
- **Active request counts** — used to break ties (least-loaded among cache-affinity candidates).
- **Predicted latency (experimental)** — an ML sidecar predicts per-request latency from KV,
  queue, and prefix-score features, removing manual weight tuning. Experimental, not default.

**Prefix-cache-aware routing has two modes:**
- *Approximate* — EPP keeps an in-memory LRU index of which prefix hashes it recently sent to
  which pods (rolling hash on fixed-size blocks, char-to-token ratio). No tokenizer sidecar, no
  external deps; can diverge from real server state. Good for homogeneous workloads.
- *Precise* — each vLLM pod publishes KV-cache events over **ZMQ**; the router subscribes and
  builds a global block-hash index, filters to pods holding the prefix, then picks the
  least-token-loaded among them. Uses vLLM's `/v1/completions/render` (HTTP render endpoint) for
  exact tokenization. ~100% token-accurate. Expected to become the default in a future release.

IGW's upstream README frames these as "Metrics and Capabilities" (Prefix Cache status, KV-cache
awareness, avoiding evictions/queueing under load); the concrete scorer implementations live in
the llm-d Router.

### 3. Integration with vLLM on EKS

- **Contract:** Deploy an `InferencePool` (IGW CRD, installed via Helm, e.g.
  `oci://registry.k8s.io/gateway-api-inference-extension/charts/inferencepool`) pointing at your
  vLLM pods, front it with a Gateway API implementation, and register the EPP via ext-proc. The
  gateway calls the EPP (Envoy external-processing callback) per request; the EPP returns the
  chosen pod address.
- **vLLM requirements:** Enable Automatic Prefix Caching (cross-request KV caching) for
  prefix-cache routing to help. Precise mode additionally needs vLLM's KV-event publishing (ZMQ)
  and the render endpoint. These are stock vLLM features — **no vLLM fork required**.
- **EKS specifics:** Both are cloud-agnostic and run on any conformant cluster; **no
  EKS-specific documentation was found** in primary sources. GKE ships a managed Inference
  Gateway using the same open-source EPP; on EKS we would self-host the gateway (Istio /
  kgateway / agentgateway) + the llm-d Router. Assumption: standard EKS + a supported gateway
  controller is sufficient — flag if we need an AWS-managed equivalent to GKE Inference Gateway
  (none confirmed to exist).

### 4. Multi-LoRA support (multiple adapters on one base model, adapter-aware routing)

**vLLM (the serving layer) — this is where multi-LoRA actually lives:**
- Start with `--enable-lora` and `--lora-modules name=path` (JSON form allows
  `base_model_name`), serving many adapters over one shared base model.
- Clients select an adapter via the request `model` field (each adapter looks like its own
  model).
- Runtime load/unload via `/v1/load_lora_adapter` and `/v1/unload_lora_adapter` (requires
  `VLLM_ALLOW_RUNTIME_LORA_UPDATING=True`), including in-place reload.
- `max_loras` controls how many adapters can be active concurrently; adapter and base-model
  requests are processed in parallel.

**Routing layer:**
- **llm-d** — added **cache-aware LoRA routing in v0.5** (adapter-aware routing alongside the
  KV/prefix scorers), plus active-active HA. This is the adapter-aware routing capability we
  want for Layer 3.
- **IGW upstream** — supports use-case-specific LoRA adapters and controlling incremental
  adapter rollout; a "recommended LoRA adapter pipeline for automated rollout" is a **roadmap
  item, not yet fully implemented.**

Net: vLLM does the multi-adapter serving; llm-d (v0.5+) does the adapter-aware routing on top.
Confirm the LoRA-routing feature against the `llm-d/llm-d-router` docs at the version we pin.

---

## Sources

- Gateway API Inference Extension — README: https://github.com/kubernetes-sigs/gateway-api-inference-extension/blob/main/README.md
- Gateway API Inference Extension — Releases: https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases
- Introducing Gateway API Inference Extension (Kubernetes blog): https://kubernetes.io/blog/2025/06/05/introducing-gateway-api-inference-extension/
- IGW EPP configuration guide: https://gateway-api-inference-extension.sigs.k8s.io/guides/epp-configuration/config-text/
- llm-d — Prefix-Cache Aware Routing (architecture docs): https://llm-d.ai/docs/architecture/advanced/kv-management/prefix-cache-aware-routing
- llm-d — Endpoint Picker (EPP) docs: https://llm-d.ai/docs/dev/architecture/core/router/epp
- llm-d — Router docs: https://llm-d.ai/docs/dev/architecture/core/router
- llm-d — Well-Lit Paths: https://llm-d.ai/docs/well-lit-paths
- llm-d — Precise Prefix Cache Aware guide (GitHub): https://github.com/llm-d/llm-d/blob/main/guides/precise-prefix-cache-aware/README.md
- llm-d — GitHub releases: https://github.com/llm-d/llm-d/releases
- llm-d 0.2 "first well-lit paths" blog: https://llm-d.ai/blog/llm-d-v0.2-our-first-well-lit-paths
- llm-d 0.3 "IGW GA" blog: https://llm-d.ai/blog/llm-d-v0.3-expanded-hardware-faster-perf-and-igw-ga
- llm-d — Predicted-Latency Based Scheduling: https://llm-d.ai/blog/predicted-latency-based-scheduling-for-llms
- llm-d — KV-Cache wins blog: https://llm-d.ai/blog/kvcache-wins-you-can-see
- Red Hat Developer — Master KV cache aware routing with llm-d: https://developers.redhat.com/articles/2025/10/07/master-kv-cache-aware-routing-llm-d-efficient-ai-inference
- vLLM — Multi-LoRA / LoRA adapters docs: https://docs.vllm.ai/en/latest/features/lora.html
- vLLM production-stack — Gateway API Inference Extension: https://docs.vllm.ai/projects/production-stack/en/latest/deployment/gateway-inference-extension.html
