<!-- Research: which serving substrate (vLLM, Ray Serve LLM, llm-d, Gateway API Inference Extension) supplies each layer L1-L4 for slipstream, current to 2026-09. -->

# Serving substrate landscape (as of 2026-09)

## Recommendation (substrate per layer)

| Layer | Recommended substrate | Rationale |
|-------|----------------------|-----------|
| **L1 — single-replica knobs** | **vLLM** (the engine, everywhere) | Every knob in the ticket is native vLLM: `--max-num-seqs`, `--max-num-batched-tokens`, `--gpu-memory-utilization`, chunked prefill, FP8/AWQ, automatic prefix caching (APC), tensor parallel. The other three substrates are orchestrators that *run vLLM as their engine* — they do not replace L1, they inherit it. |
| **L2 — cluster platform** | **Kubernetes-native (Karpenter + HPA/KEDA on inference metrics)**, with the routing layer emitting the signals; **or** Ray Serve LLM if you accept a second scheduler | Because slipstream is EKS, the idiomatic path is K8s HPA/KEDA driven by inference signals (queue depth, KV-cache pressure) from the Gateway/EPP, and Karpenter for spot + node scaling. Ray Serve LLM also does app-level autoscaling and scale-to-zero, but adds a parallel control plane (KubeRay + Ray autoscaler) on top of K8s. |
| **L3 — inference-aware routing** | **Gateway API Inference Extension (IGW) / llm-d Router** | IGW is **GA** in 2026 and its EndpointPicker (EPP) does prefix-cache-aware routing natively; llm-d layers a global KV-cache indexer on top. Multi-LoRA on a shared base is served by the vLLM engine and supported by both llm-d and Ray Serve LLM. Ray Serve LLM's `PrefixCacheAffinityRouter` is the equivalent if you go the Ray route. |
| **L4 — P/D disaggregation + KV offload** | **vLLM engine (KV connectors + LMCache) orchestrated by llm-d on EKS**, or by Ray Serve LLM if you standardise on Ray | The actual disaggregation and KV transport live in **vLLM** (NIXL / LMCache connectors) — both llm-d and Ray Serve LLM drive that same machinery. llm-d is K8s-native (P/D pools as one `InferencePool`, LMCache tier, composes with the L3 router). Ray Serve LLM's `PDProxyServer` does the same inside Ray. |

### Verdict on the brief's lean ("vLLM engine + Ray Serve LLM for L4")

**Partially validated, with a challenge.** Ray Serve LLM genuinely delivers L4: it has real P/D disaggregation (`PDProxyServer`), prefix-cache-aware routing (`PrefixCacheAffinityRouter`), multi-LoRA, and it is deeply integrated with the vLLM engine. If the team wants **one framework** for L2+L3+L4 and is comfortable running Ray on Kubernetes, it is the fastest coherent path.

The challenge: slipstream is an **EKS-native** platform. Ray Serve LLM introduces a **second control plane and autoscaler** (KubeRay + Ray autoscaler sitting above the K8s/Karpenter autoscaler) and a Ray-specific routing model. The **llm-d + Gateway API Inference Extension** stack is Kubernetes-native, uses standard CRDs and the (GA) Inference Extension, was donated to the **CNCF Sandbox (Mar 2026)**, and composes P/D disaggregation, prefix-aware routing, LMCache KV offload, and K8s HPA/KEDA autoscaling without a parallel scheduler. For an EKS platform whose other layers are already K8s objects, llm-d is the more idiomatic L2-L4 substrate and avoids double autoscaling.

Both paths use **vLLM as the engine and the same vLLM KV-transfer/LMCache primitives underneath**, so the L4 decision is really "which orchestrator drives vLLM's disaggregation," not "which engine disaggregates."

## Capability matrix

Legend: ✅ native / first-class · ⚙️ via the vLLM engine it wraps · 🟡 experimental/alpha/immature · ❌ not a goal of this substrate · — n/a

| Capability | vLLM (engine) | Ray Serve LLM | llm-d | Gateway API Inference Ext. (IGW/EPP) |
|---|---|---|---|---|
| **L1** APC prefix caching | ✅ | ⚙️ (vLLM) | ⚙️ (vLLM) | — (routing layer, not an engine) |
| **L1** chunked prefill | ✅ | ⚙️ | ⚙️ | — |
| **L1** FP8 / AWQ quant | ✅ | ⚙️ | ⚙️ | — |
| **L1** tensor/pipeline/expert parallel | ✅ | ✅ (also Wide-EP orchestration) | ⚙️ | — |
| **L2** replica autoscaling | — | ✅ (`num_replicas="auto"`) | ✅ (HPA/KEDA on IGW metrics; WVA) | 🟡 (emits metrics; HPA support on roadmap) |
| **L2** scale-to-zero | — | ✅ (`min_replicas: 0`, cold-start tradeoff) | 🟡 (activator; `minReplicas:0` alpha, or KEDA) | ❌ (delegated to HPA/KEDA) |
| **L2** node scaling / spot | — | 🟡 (Ray autoscaler requests nodes; spot-preferring scale-down is an open RFE, late-2025) | via K8s (Karpenter/KEDA); no substrate-specific spot logic found | via K8s |
| **L3** prefix-cache-aware routing | ❌ (single replica) | ✅ (`PrefixCacheAffinityRouter`) | ✅ (KV-cache indexer + approximate/precise EPP scorers) | ✅ (`prefix-cache-scorer` plugin in EPP) — **GA** |
| **L3** multi-LoRA on one base | ✅ (engine feature) | ✅ | ✅ (LoRA-aware scheduling in v0.5) | ⚙️ (routes to LoRA-capable vLLM pods; adapter pipeline on roadmap) |
| **L4** P/D disaggregation | 🟡 **experimental** (9 KV connectors: NIXL, LMCache, Mooncake, Offloading, Multi, …) | ✅ (`PDProxyServer`, separate prefill/decode deployments; drives vLLM KV transfer) | ✅ (P/D as one `InferencePool`, routing-proxy sidecar; composes with EPP scorers) | ❌ today (disaggregated pools on roadmap) |
| **L4** KV-cache offload / LMCache (CPU/NVMe/remote) | ✅ (`--kv-offloading-backend lmcache`; LMCache backends: CPU, disk/NVMe, Redis, S3) | ⚙️ (via vLLM engine args) | ✅ (LMCache tier, Redis-backed shared cache, NIXL/UCCL transport in v0.5) | ❌ (roadmap: interfaces for remote caches) |

Notes on maturity that matter:
- **vLLM P/D disaggregation is explicitly experimental** and the docs state it "DOES NOT improve throughput" — it is a **latency/TTFT** optimization that lets prefill and decode scale independently. Treat L4 as a latency play, not a throughput win.
- **IGW is GA** (2026); its community meetings have merged into the **llm-d Router** meeting, i.e. IGW and llm-d's router are effectively the same effort.
- **llm-d** was donated to **CNCF Sandbox (24 Mar 2026)**; WVA autoscaler was experimental in v0.4, iterated in v0.5; scale-to-zero uses an alpha feature gate (or KEDA). Latest referenced release: **v0.6.0 (3 Apr 2026)**.
- **Ray Serve LLM** docs do not flag its L3/L4 features as experimental, but **spot-aware scale-down is an open RFE** (Nov 2025), so treat spot handling as DIY.

## Cost of mixing

1. **vLLM is non-negotiable and shared.** All three orchestrators run vLLM (Ray Serve LLM also supports SGLang; llm-d/IGW are engine-agnostic but the KV-indexer routing assumes vLLM APC + KV-event emission). L1 is settled the moment you pick vLLM. No mixing cost at L1.

2. **Two autoscalers is the main tax.** Choosing Ray Serve LLM for L4 while keeping the rest K8s-native means the Ray autoscaler and Karpenter/HPA both make placement decisions. Scale-to-zero, spot reclaim, and node bin-packing then have two owners — a real operational hazard on EKS. Picking one plane (Ray *or* K8s/llm-d) for L2+L4 avoids it.

3. **Routing must match the engine's cache reality.** Prefix-cache-aware routing (L3) only pays off if the router's view of KV-block locality matches vLLM's. llm-d's *approximate* indexer needs no engine changes; its *precise* path needs vLLM KV-event emission + ZMQ + a render endpoint (extra infra, 100% precision). Mixing a generic gateway with vLLM without wiring the prefix scorer to the engine gives you round-robin dressed up as smart routing.

4. **P/D disaggregation buys TTFT, not throughput, and costs transport complexity.** Both llm-d and Ray Serve LLM sit on vLLM's experimental disaggregation. Tail latency is then governed by KV-cache transport (NIXL over UCX/RDMA; llm-d v0.5 added the UCCL backend). On EKS this means EFA/RDMA-capable node pools and careful PCIe/NIC sizing; LMCache's benchmarks show it beating vLLM-native CPU offload (~400 vs ~88 Gbps) but also warn PCIe can be the bottleneck for agentic workloads. Do not enable L4 before L1-L3 are saturating a single pool.

5. **LMCache is the portable KV-offload layer.** It plugs into plain vLLM (`--kv-offloading-backend lmcache`), into llm-d (MultiConnector = NixlConnector for the P→D handoff + LMCacheMPConnector for cross-request reuse), and can be driven from Ray Serve LLM via engine args. So the L4 KV-offload choice is not locked to the orchestrator.

## Sources

- vLLM — Disaggregated Prefilling (experimental): https://docs.vllm.ai/en/stable/features/disagg_prefill/
- vLLM — LMCache example / KV offload: https://docs.vllm.ai/en/v0.10.1/examples/others/lmcache.html
- vLLM — disaggregated prefilling & KV transfer roadmap (RFC #10818): https://github.com/vllm-project/vllm/issues/10818
- Ray Serve LLM — Serving LLMs (index): https://docs.ray.io/en/latest/serve/llm/index.html
- Ray Serve LLM — core components / vLLM integration: https://docs.ray.io/en/latest/serve/llm/architecture/core.html
- Ray Serve — Autoscaling guide (scale-to-zero, num_replicas="auto"): https://docs.ray.io/en/latest/serve/autoscaling-guide.html
- Ray Serve — Advanced autoscaling: https://docs.ray.io/en/latest/serve/advanced-guides/advanced-autoscaling.html
- Ray Serve — customizable scale-down / spot RFE (#58959): https://github.com/ray-project/ray/issues/58959
- Anyscale — Ray Serve LLM Wide-EP & disaggregated serving with vLLM: https://www.anyscale.com/blog/ray-serve-llm-anyscale-apis-wide-ep-disaggregated-serving-vllm
- llm-d — Prefix-cache-aware routing: https://llm-d.ai/docs/architecture/advanced/kv-management/prefix-cache-aware-routing
- llm-d — Disaggregated serving: https://llm-d.ai/docs/dev/architecture/advanced/disaggregation
- llm-d — Workload autoscaling (HPA/KEDA, WVA, scale-to-zero): https://llm-d.ai/docs/guide/Installation/workload-autoscaling
- llm-d — v0.5 "Sustaining Performance at Scale" (UCCL, LoRA, scale-to-zero): https://llm-d.ai/blog/llm-d-v0.5-sustaining-performance-at-scale
- llm-d — Workload Variant Autoscaler repo: https://github.com/llm-d/llm-d-workload-variant-autoscaler
- Red Hat Developer — KV-cache-aware routing with llm-d: https://developers.redhat.com/articles/2025/10/07/master-kv-cache-aware-routing-llm-d-efficient-ai-inference
- Gateway API Inference Extension — intro/docs: https://gateway-api-inference-extension.sigs.k8s.io/
- Gateway API Inference Extension — repo (GA status): https://github.com/kubernetes-sigs/gateway-api-inference-extension
- Gateway API Inference Extension — EPP config (prefix-cache-scorer): https://gateway-api-inference-extension.sigs.k8s.io/guides/epp-configuration/config-text/
- Kubernetes blog — Introducing Gateway API Inference Extension: https://kubernetes.io/blog/2025/06/05/introducing-gateway-api-inference-extension/
- LMCache — Disaggregated prefill: https://docs.lmcache.ai/mp/disaggregated_prefill.html
- LMCache — paper (KV cache layer, offload bandwidth benchmarks): https://arxiv.org/pdf/2510.09665
