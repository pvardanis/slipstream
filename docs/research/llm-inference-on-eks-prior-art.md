<!-- Distilled decisions from the aws-samples/sample-llm-inference-on-eks repo, mapped onto slipstream's L0–L4 layers. Prior art, not a slipstream decision: reference to mine when building each layer, saying where AWS aligns with, differs from, or has no counterpart to slipstream's plan. Decisions slipstream actually takes live in docs/adr/ and spec.md §11. -->

# LLM inference on EKS — prior art (aws-samples)

**Date**: 2026-09-14
**Source**: [`aws-samples/sample-llm-inference-on-eks`](https://github.com/aws-samples/sample-llm-inference-on-eks),
pinned at commit `56487b7` (2026-09-09). The repo is external and drifts; every claim below
cites the file it came from (`aws:<path>`) so it can be re-checked or re-pulled. This is a
snapshot, not a subscription — re-fetch to refresh.

## TL;DR / Recommendation

A production-grade build of slipstream's own roadmap: EKS + Karpenter GPU node pools,
vLLM/SGLang engines, KEDA-less autoscaling, PD disaggregation over EFA — running the flagship
end (8× H200 `p5en`, TP8/TP16, 750B MoE) slipstream deliberately isn't. So its **architecture**
transfers as a template with a scope gap (their multi-node/EFA/NIXL machinery is heavier than a
single A10G needs), while its **epistemic discipline transfers whole and matters now**.

The repo's loudest, most repeated lesson is not architectural. Every benchmark number in it is
n=1, ramp-up-contaminated, and the *direction* of the measurement bias flips between rigs
(TTFT understated 6–14× on one, throughput overstated 53% on another). Its root `CLAUDE.md` rule
— **"no causal claim without evidence in hand; a suspicious number is a stop sign"** — is the
thing to adopt before any node class. slipstream ships numbers people act on starting at L0;
this is the standard those numbers have to clear.

Recommend: adopt the benchmark-methodology discipline (§ Epistemic) into slipstream-bench now;
treat §§ L1–L4 as a checklist to consult when building each layer, not a spec to copy.

## Epistemic discipline — applies to every layer, bites at L0 now

From `aws:CLAUDE.md` ("Never Attribute Without Evidence"), `aws:docs/BENCHMARK-METHODOLOGY.md`,
`aws:docs/BENCHMARK-REPORTING-PRINCIPLES.md`. slipstream-bench uses `vllm bench serve`, not
genai-perf, so the *flags* don't port — the methodology does:

- **A shallow run is a mis-measurement, not a conservative one.** genai-perf/perf_analyzer
  default to `max(10, 2×concurrency)` requests — ~2 per concurrency slot — which measures the
  queue-filling transient, not steady state. Which metric that ruins, and in which direction, is
  rig-specific: TTFT understated 6–14× on L40S/Qwen3-8B, *overstated* 1.9–2.8× on B300/Kimi-K3,
  throughput overstated 53% on H200/GLM-5.2. You cannot argue "the real value is only
  better/worse than this." → slipstream's L0/L1 sweep charts need a steady-state bound and a
  variance estimate or they mislead.
- **The stability flag decides when the tool stops, not what it reports.** `--stability-percentage`
  gates termination; genai-perf then averages *every* window including ramp-up. Steady state
  requires trimming leading windows by hand. → whatever `vllm bench serve` reports, confirm it's
  over a settled window, not the whole run.
- **Drain the server to idle between runs.** Back-to-back runs inherit the prior backlog; on one
  AWS rig that contamination (34% on p50) exceeded the effect under study.
- **n=1 blocks a cross-arm claim.** Every published AWS figure is single-run with no variance;
  they hold their own comparisons as provisional because of it. slipstream's knob-sweep (L1),
  routing crossover (L3), and PD-vs-colocated (L4) are all cross-arm claims — repeat runs.
- **Separate observed / inferred / hypothesised, and don't attribute cause without a controlled
  A/B or profile.** AWS withdrew a "decode idles" PD conclusion that was an inference from two
  throughput numbers with no per-node telemetry. slipstream's spec already asks for "an honest
  'where it didn't pay'" at L4 — this is the standard that phrase has to meet.

## L1 — tuned replica

Engine knobs, from `aws:k8s-manifest/vllm/glm-5.2-fp8-p5en-vllm.yaml` and
`aws:docs/KV-CACHE-ARCHITECTURE.md`:

- **`--gpu-memory-utilization` 0.85, not the 0.9 default** — 0.9 OOM-crashed under 8K-input load;
  SGLang's equivalent (`--mem-fraction-static`) held at 0.80, not 0.85. Aligns with the KB note
  (`vllm-internals.md`) that vLLM V1 profiles the forward pass to measure real free VRAM — trust
  the profile, then leave headroom. Directly a slipstream L1 sweep knob.
- **FP8 KV cache** (`--kv-cache-dtype=fp8`) is their default — matches the slipstream rig exactly.
- **LMCache offload wins big but only within its pool.** AWS measured LMCache L1 host-RAM offload
  at 8–22× hot-TTFT vs cold (single run), and 22× vs 1.14× for vLLM's native OffloadingConnector.
  But the benefit is zero once the working set exceeds the pool (LRU evicts it) — both cases
  measured. slipstream lands LMCache first at L1 (spec §"LMCache lands first") for exactly this
  cheap-to-debug reason; the pool-size cliff is the thing to characterise.
- **Probe shape for slow weight load** — startupProbe `failureThreshold: 180 × periodSeconds: 30`
  = 90 min budget for weight download; readiness (HTTP `/health`) and liveness (TCP) split so a
  loading pod isn't killed. slipstream's Qwen3-8B AWQ (~5 GB) loads far faster than their 750 GB,
  but the startup-vs-liveness split is the pattern L2a cold-start measurement will need.
- **`strategy: Recreate`** — one GPU can't host old+new replica during a rollout. slipstream's
  `k8s/vllm.yaml` already does this; it stays true when the replica moves to GPU.

## L2 — platform (Karpenter, spot, cold start)

From `aws:infrastructure/terraform/kubernetes/karpenter/{node-pools,node-classes}/gpu.yaml`,
`aws:infrastructure/terraform/CLAUDE.md`, `aws:k8s-manifest/infra/`:

- **NodePool: `capacity-type` In `[spot, on-demand]`, `instance-family` includes `g5`, GPU taint
  `nvidia.com/gpu` with pods tolerating `operator: Exists`.** The Exists toleration is what keeps
  model manifests portable across node pools — slipstream should adopt it so its CPU-vs-GPU
  manifests stay one file. Disruption `consolidationPolicy: WhenEmpty` + `consolidateAfter` is how
  empty GPU nodes get reclaimed (their scale-to-zero-ish); slipstream's duty-cycle down-scaling
  rides the same mechanism.
- **EC2NodeClass `amiSelectorTerms: alias: al2023@latest` — don't hand-pin GPU AMIs.** The alias
  resolves to the NVIDIA AL2023 variant on its own from the instance's GPU-count requirement.
  Hand-pinning silently omits new families.
- **GPU Operator with `driver.enabled=false` and `toolkit.enabled=false`** — the AL2023 NVIDIA AMI
  already ships driver + container toolkit; a containerized driver collides with the baked-in one.
  slipstream will hit this the moment it adds the GPU node group — the Operator is device-plugin
  only here.
- **`instanceStorePolicy: RAID0`** is what lands the `hostPath` weight cache on local NVMe;
  removing it pushes downloads onto the EBS root volume. Weight-cache-on-NVMe → fast restart is
  the cold-start lever (slipstream L2a), even though a 5 GB AWQ model makes the stakes smaller
  than their 750 GB.
- **Invariants that will bite slipstream's GPU bring-up:**
  - **GPU service quota is often `0` on a fresh account** — Karpenter then *silently* fails to
    provision. Check `L-DB2E81BA` (G instances) before first GPU `just cluster-up`.
  - **`nodeRepair` feature gate OFF** — it misread long model loading (JIT compile) as an
    unhealthy node and terminated GPU nodes minutes after launch. slipstream's cold-start *is* a
    long load; this is a direct trap.
  - **VPC endpoints (S3 gateway + ECR interface)** so private-subnet pods don't pay NAT egress for
    image pulls. slipstream's `terraform/eks` runs a single NAT; the vLLM image pull on every
    cold start crosses it. Worth an endpoint before L2a timings.
  - **An extra AZ beyond the usual three** — newest GPU on-demand capacity often lands only in the
    4th AZ; for slipstream the analogous risk is `g5` *spot* availability per AZ.
  - **`high-priority-100` PriorityClass** on model-serving pods (`aws:k8s-manifest/infra/priority-class.yaml`).
- **Gap — no autoscaling prior art.** The AWS repo has no HPA/KEDA; scaling is Karpenter node
  consolidation only. slipstream's L2a (KEDA/HPA on `num_requests_waiting`) is net-new here — no
  template to lean on, and the KB has no autoscaling doc either. Build it from the KEDA/vLLM
  metrics primary sources, not from this repo.

## L3 — inference-aware routing

**Largely a gap.** The AWS repo routes only inside PD disaggregation, via `sglang-router`
(`aws:docs/PD_DISAGGREGATION.md`) — it does **not** use the Gateway API Inference Extension or
llm-d Router/EPP that slipstream's L3 targets (spec §"L3/L4"). No template here for `InferencePool`
+ EPP; build L3 from the IGW/llm-d sources (see `docs/research/inference-routing.md`).

The one transferable finding reinforces a caution slipstream already holds: from
`aws:docs/KV-CACHE-ARCHITECTURE.md`, cache benefit is full when the working set fits the pool and
**zero when it doesn't** (LRU evicts). That is the mechanism behind slipstream's own L3 note that a
`prefix_repetition` workload makes cache-aware routing "win by construction" — the crossover curve
the spec demands is exactly the working-set-vs-pool boundary.

## L4 — prefill/decode disaggregation

From `aws:docs/PD_DISAGGREGATION.md`, `aws:k8s-manifest/lws/`:

- **AWS shape: LeaderWorkerSet + NIXL KV transfer over EFA RDMA**, one prefill node + one decode
  node + a router that health-gates *both* roles (init container polls both `/health`, up to
  40 min) before starting. In a 1P+1D shape the *only* traffic crossing EFA is the prefill→decode
  KV transfer; TP collectives stay on NVLink.
- **Scope gap vs slipstream.** slipstream's L4 is separate prefill/decode node pools with **LMCache**
  KV transfer on single A10G nodes — no EFA, no NIXL, no multi-node TP, no LWS. So the AWS
  transport machinery doesn't port; what ports is the *operational* shape (a router that won't
  admit traffic until both roles are live) and the KV-connector-as-transfer-substrate idea, which
  slipstream already plans to inherit from its L1 LMCache work (spec §"LMCache lands first").
- **Cautionary precedent — the honest result.** AWS's one measured PD comparison is n=1 per arm,
  ramp-up-included, and its earlier "1P:1D loses on prefill-heavy traffic because decode idles"
  conclusion was **withdrawn** — the throughput gap came from under-depth sampling and no per-node
  utilisation/queue telemetry was ever collected, so "decode idles" was an unbacked inference. Both
  the AWS root `CLAUDE.md` and slipstream's own spec ("an honest 'where it didn't pay'") demand the
  same thing: to claim PD economics, collect per-node telemetry and repeat runs — don't infer from
  two throughput numbers.
- **Evidence-discipline example worth noting:** AWS measured that `privileged: true` is *not* what
  grants EFA access (NCCL still selected EFA after removing it) — irrelevant to slipstream (no EFA)
  but a model of testing the mechanism instead of assuming it.

## What deliberately does not transfer

The AWS flagship targets 8× H200 `p5en`, TP8/TP16, 750B–2.8T MoE models, SGLang-primary,
multi-node EFA. slipstream's rig is one A10G, no tensor parallelism, Qwen3-8B AWQ, vLLM-only. So
their per-model engine tuning (SGLang `--mem-fraction-static 0.80`, NIXL/EFA/LWS multi-node,
capacity-block/ODCR node classes) is out of slipstream's scope until far past L4 — noted here so
future layers don't cargo-cult it.
