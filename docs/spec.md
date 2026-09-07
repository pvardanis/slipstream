<!-- The central slipstream platform spec: assembles every resolved wayfinder decision into one build-ready document. Detailed enough to start Layer 0 and to sequence the phases that follow. -->

# slipstream — platform spec

A self-hosted LLM inference platform on AWS EKS: open models on spot GPUs, with the
**cluster-level machinery around the model** as the focus — autoscaling on inference
signals, spot-eviction survival mid-stream, cold-start elimination on large weight loads,
inference-aware routing, and prefill/decode disaggregation. `just up` / `just down` fully
create and destroy GPU capacity. It runs cost-competitive against commercial APIs and proves
it on a live scoreboard.

This is a **planning spec**, detailed enough to begin building Layer 0 and to sequence the
phases after it. Deeper per-phase experiment design is deferred to build-time (see
[§10](#10-open-build-time-decisions)). Provenance for every decision is in
[§11](#11-decision-log).

---

## 1. Thesis and the gap

The engineer already owns what runs *inside* the pod — vLLM, continuous batching, token
streaming, benchmarking. The gap this platform closes is everything *around* it at cluster
scale: autoscaling on inference signals, cold start of large weights, spot-eviction survival,
inference-aware routing, and prefill/decode disaggregation. The output is **public evidence**
(a repo with real commit history and write-ups), not a running product.

**Binding constraint: focus, not time or money.** The likeliest failure is breadth creep —
six half-finished layers. Every decision below favours depth-first over breadth-first.

---

## 2. Constraints and operating budget

- **Effort:** ~30 hrs/week.
- **GPU budget:** ~$100–250/mo. This is a **duty-cycle budget**, not 24/7 — even the cheapest
  24 GB card run continuously is ~$365/mo. Meaningful daily uptime under $250/mo means
  **scale-to-zero** and ~7–17 hrs/day of active GPU time. Teardown discipline is a design
  requirement: `just down` must return GPU spend to zero.
- **Cloud:** AWS only — chosen precisely because the cluster-orchestration gap is the point.
- **IaC:** Terraform-only, never the console.
- **Developable on CPU:** the platform must stand up against a tiny CPU model so GPU hours go to
  measurement, not YAML debugging.

---

## 3. Locked decisions (the build substrate)

### 3.1 GPU, model, serving substrate

- **Instance:** `g5.xlarge` — 1× NVIDIA A10G (24 GB), **spot**, with autoscale-to-zero. Best
  spot value of the 24 GB cards (~$0.44–0.64/hr `us-east-1`). A single card is the target: one
  48 GB card would beat 2-GPU tensor parallel, and TP needs both GPUs in one instance — but 8B
  fits one A10G, so **no tensor parallelism** (this supersedes the brief's "L1 = TP-across-2-GPU").
- **Model:** **Qwen3-8B (dense), AWQ-INT4 (`awq_marlin` kernel), FP8 KV cache.** Chosen for
  concurrency headroom, not coding quality. **FP8 *weights* are rejected** — Ampere (A10G) has no
  hardware FP8, so INT4 AWQ is the right quantization for this card. FP8 KV cache is still used
  (it roughly doubles concurrent-sequence capacity).
- **Concurrency envelope:** AWQ-INT4 weights (~5.5 GB) + FP8 KV on 24 GB gives a worst-case
  ~50–60 sequences at full 4k context; live chat traffic (shorter average generations) runs
  higher. The brief's "~50 seqs @4k" is realistic under exactly this config — not for BF16
  weights, which strict 4k math caps near ~9.
- **Substrate per layer:**
  - **L1 (single replica):** **vLLM** — every knob is native (`--max-num-seqs`,
    `--max-num-batched-tokens`, `--gpu-memory-utilization`, chunked prefill, AWQ, FP8 KV,
    automatic prefix caching). The other substrates orchestrate vLLM; they do not replace it.
  - **L2 (platform):** **Kubernetes-native — Karpenter (spot + node scaling) + KEDA/HPA on
    inference signals** (`num_requests_waiting`, KV-cache pressure). Not Ray Serve, which would
    add a second control plane and autoscaler on EKS.
  - **L3/L4 (routing + disaggregation):** **Gateway API Inference Extension (IGW, GA) as the
    routing contract + a version-pinned pre-1.0 llm-d Router/EPP as the scheduler.** These are
    two layers of one stack, not rivals. Both drive **unmodified vLLM**.
- **Artifact pinning (so a number reproduces):** every published measurement pins the **weight-blob
  checksum, vLLM version, and quant recipe** (`awq_marlin` + FP8 KV). A re-run months later must
  reproduce the figure or the write-up is not evidence.

### 3.2 Benchmark harness (Layer 0)

- A **thin wrapper over `vllm bench serve`** — not a bespoke load generator.
- **Workload:** vLLM-native `prefix_repetition` + `--burstiness` (sweep prefix-share % and burst).
  A real trace is a one-time realism check only, not the primary workload — but the check is
  **quantified**: run the real trace once and report the **delta %** between it and the synthetic
  workload's headline numbers, so "we validated realism" becomes a figure, not a claim.
- **SLO:** **TTFT p95 ≤ 1000 ms, TPOT ≤ 50 ms** — expressed to the harness as
  `--goodput ttft:1000 tpot:50`. Report **p99 alongside p95** (spot + batching produce fat tails a
  p95 hides).
- **Build only two thin pieces** on top of vLLM flags: a **cost-per-1M-tokens post-processor**
  and a **server-side prefix-cache-hit-rate scraper joined to the client JSON**. Everything else
  is vLLM configuration.

**Measurement discipline** (what keeps the public numbers honest):

- **Segment, don't blend.** Report goodput / TTFT p95 **per concurrency level and per prefix-share
  bucket**, not one aggregate p95 — a single number hides "meets SLO at 20 seqs, blows it at 55,"
  which is the story.
- **Price input and output separately.** Commercial APIs charge output tokens ~3–4× input, so a
  blended self-hosted $/1M vs blended commercial $/1M lies. Report **$/1M-input and $/1M-output**,
  or pin the fixed input:output ratio used for the blend.
- **Cold vs warm cache, both labelled.** Prefix-cache warmth swings throughput hard; a run long
  enough to warm the cache reports numbers first traffic never sees. Report **cold (empty cache)
  and steady-state (warm) SLO** — the inference analog of a train/test boundary.
- **Measure the commercial baseline, don't quote it.** The cost scoreboard's commercial arm is the
  **same L0 workload fired at the commercial API** and measured (real TTFT/TPOT/cost), never a list
  price off a docs page. Apples-to-apples or it is not a baseline.

---

## 4. Phase plan (L0 → L4)

The order is a **forced dependency chain** — each layer's prerequisite is the prior layer
(harness → tuned replica → autoscaling/spot → routing → disaggregation). No reorder freedom
exists; the design freedom is in granularity, definition of done, and where it stops.

**Six phases.** L2 splits in two because it carries the most weight and each half ships
independently.

| Phase | Scope | Done when | Publishable measurement |
|---|---|---|---|
| **L0** — harness | `vllm bench serve` wrapper + cost/1M post-processor + prefix-cache-hit scraper | all three run and join | Baseline **$/1M self-hosted vs commercial API at SLO** (TTFT p95 ≤1000, TPOT ≤50), one config |
| **L1** — tuned replica | knob sweep: `max-num-seqs` · KV quant · chunked prefill · prefix cache · **LMCache offload** | sweep hits SLO at max concurrency | **Knob-sweep chart:** max sustained concurrency at SLO + the LMCache cache-extension win |
| **L2a** — cold start + autoscale | cold-start-from-zero (image pull + weight load) measured; KEDA/HPA on `num_requests_waiting`; Karpenter provisioning | scales up under load from zero | **Cold-start latency breakdown + autoscaling-under-load timeline** |
| **L2b** — spot + chaos | scale-to-zero + wake; spot-eviction drain; **chaos day** (evict node mid-generation, token stream survives) | a generation survives a spot eviction | **"vLLM on EKS spot: cold start, autoscaling, what breaks"** write-up + chaos-day survival capture |
| **L3** — routing + obs spine | IGW + llm-d Router/EPP; prefix-cache-aware routing vs round-robin **across a prefix-share sweep**; request-ID join spine live | the sweep reports a crossover point, pivot demo works | **"Prefix-cache-aware routing vs round-robin"** write-up (a curve, not a point — where cache-aware routing wins, and where it stops) + the cross-tool pivot demo |
| **L4** — P/D disaggregation | separate prefill/decode node pools; **LMCache KV transfer** between them; measured vs colocated | disagg runs and is measured | **P/D disaggregation result**, including an honest "where it didn't pay" |

Note on L3: the routing eval must not win by construction. A workload built from
`prefix_repetition` makes cache-aware routing beat round-robin almost tautologically, so L3
**sweeps prefix-share % and reports the crossover** — the point below which cache-aware routing
stops paying, including where it can lose. One real-trace arm anchors the sweep to reality. The
deliverable is that curve, not a single rigged data point.

Note on L4: vLLM's disaggregation is a **latency/TTFT** play, not a throughput win, and its tail
latency is governed by KV-cache transport (NIXL/UCCL over EFA/RDMA-capable node pools). Do not
enable L4 before L1–L3 are saturating a single pool.

Note on failure analysis (every phase): the deliverables are honest about what breaks, not just
what works.
- **Cohort the failures.** `generation_failed` is already tagged spot-evict / OOM / timeout —
  report a **breakdown per phase**: which reason dominates at which concurrency.
- **Capture a failure, not only the save.** Chaos day captures a surviving stream; also capture the
  request that **died** (the OOM at max concurrency, the eviction before drain works). The honest
  failure is the more valuable artifact.
- **Attribute the tail.** For SLO-missing requests, split latency into **queue-wait vs prefill vs
  decode vs transport** using the request-ID spine's Sentry spans (§5.1) — this is error analysis,
  not another metric.

### 4.1 LMCache placement

**LMCache lands first in L1, and L4 inherits it.** LMCache is a Python library integrated into
the vLLM serving process (KV-connector interface) plus an optional standalone cache-server for
the distributed case. It offloads KV blocks to CPU RAM / NVMe so evicted prefixes are reused, not
recomputed. Landing it on the single L1 replica makes the offload mechanics cheap to debug and
measure in isolation; L4 then reuses the same machinery as the prefill→decode KV-transfer
substrate. Introducing it first at L4 would mean debugging offload and disaggregation at once.

### 4.2 Graceful-degradation ladder

There is **no hard minimum** — the sequence must degrade gracefully, each phase a clean
shippable stop.

```
L0 → L1 → L2a → L2b (thesis-complete floor) → L3 (bonus) → L4 (bonus)
```

- **L2b is the "if I only reach here, it succeeded" line.** The thesis is cluster-level
  orchestration around the server, and the gap is L2 — so surviving spot eviction with
  autoscaling proves the central claim and stands alone even if routing/disaggregation never ship.
- **Cut top-down: L4 first, then L3.** Never start L4 unless the spine is solid enough to finish
  it — a half-built L4 is worse than none (depth-first).
- Conscious tension accepted: **L4 is the most differentiating yet the most cuttable.** Correct
  under depth-first — it is highest-risk/highest-reward, attempted only when everything under it
  is done.

---

## 5. Observability design

Three tools, three deliberately distinct layers, joined by one request ID. Effort ceiling ≈
**one fifth of total**; the named failure mode is this becoming SDK plumbing instead of inference
depth.

- **Grafana — "is the fleet healthy?"** GPU utilization, KV-cache occupancy,
  `num_requests_waiting`, TTFT p95, Karpenter scaling events, tokens/sec/$. Home of the cost
  scoreboard and SLO burn-rate alerts. Also a **cost-drift alert**: $/hr over the duty-cycle
  budget pages — the guard against a forgotten GPU after a failed `just down`, which is the only
  real-money failure mode here.
- **Sentry — "what broke, and where in the code path?"** Instrument the router/gateway; trace
  gateway → routing decision → vLLM engine → first token → completion. CUDA OOMs, timeouts, spot
  evictions as fingerprinted issues.
- **PostHog — "what are users doing, and what did it cost?"** Session behaviour, model usage,
  cost per active user (see [§5.2](#52-posthog-and-the-thin-client)).

### 5.1 The request-ID join (the differentiator)

The **irreducible spine** is one `request_id` stamped on the **Sentry span + PostHog event +
structured log line**, with **one tested pivot path**: PostHog funnel drop-off → Sentry trace →
Loki line. All four sinks (Sentry, PostHog, Prometheus exemplar, Loki) are reachable, but the
acceptance bar is that one worked pivot, not four-way completeness.

- **Transport:** a **minimal OTel Collector** (receive OTLP, fan out, batch only — no
  tail-sampling). vLLM emits OTel traces.
- **Deploy → Sentry release marker:** a deploy event creates a Sentry release + commit SHA so a
  p95 regression attributes to the exact rollout. **Deployer-agnostic** — the marker rides
  whatever CD mechanism L2a picks (not assumed to be Argo CD).
- **Rehearsed rollback:** rollback is a **run deliverable**, not an assertion — deploy a bad config,
  watch the SLO burn-rate alert fire, revert, confirm recovery. Rides alongside chaos day. Rollback
  you have not run is not rollback.
- **Alert → runbook:** each alert names its next step (dashboard → pivot → likely cause). The
  chaos-day and rollback rehearsals **are** runbook entries one and two — a solo maintainer's
  durable memory.
- **Deliberate non-monitors:** no quality drift (no quality eval), no retrieval drift (no RAG), no
  model drift (fixed model). Stated so absence reads as decision, not omission.
- **Privacy:** shape-only. `request_id`, token counts, `prefix_hash`, `model_id`. **No raw prompt
  content leaves the cluster, ever.**

**Cut order (enforces the ~1/5 ceiling; cut top-down):**
1. Prometheus exemplars → downgrade to timestamp correlation.
2. OTel Collector → direct SDK export, if the Collector costs more than an afternoon.
3. Deploy → Sentry release marker.

**Never cut** the spine: one `request_id` on Sentry + PostHog + structured logs, with one
documented pivot path. Grafana stays timestamp-correlated in the fully-cut state.

### 5.2 PostHog and the thin client

PostHog is product analytics, and the platform has no real users — resolved by building a
**ruthlessly thin client**, a test fixture rather than a product:

- **Streamlit + `posthog-python`** (server-side, explicit events). Deliberately **not**
  `posthog-js` autocapture — autocapture grabs content/DOM and fights the shape-only privacy
  stance. No auth, no multi-user, no server-side persistence beyond PostHog events.
- **Ingress boundary:** the gateway the thin client hits is **not publicly exposed** (private VPC /
  auth in front) — an open GPU endpoint on spot is someone else's free inference. State the
  boundary explicitly even for a demonstrator.
- **PostHog's role:** the request-ID join is the anchor; **feature-flag A/B** for serving configs
  (spec-decode on/off, quant variants) rides second. The A/B measures throughput, cost, and
  latency — not output quality (the model is chosen for concurrency headroom, and no quality eval
  is in scope). Note the A/B's flag-driven traffic split **is** the canary/shadow machinery, used
  for **experiment intent** ("which config is better?"), not as a deploy safety gate ("is the new
  version not-worse?") — canary/shadow proper are n/a (no live users, single-version demonstrator).
- **Data source:** both — Danny drives the box by hand for the real qualitative funnel; a **replay
  script wraps the L0 harness workload** (`prefix_repetition` + `--burstiness`) to fire synthetic
  sessions for statistics. One traffic generator, two entry points.
- **Sequencing:** PostHog events, funnel, and flag harness are an **L3 deliverable** — the gateway
  they instrument is built at L3. The OTel/request-ID backbone is stubbed earlier.
- **Effort cap:** ≤ 1/3 of the observability slice, with a fixed deliverable list — client +
  replay + join wired + **one** funnel + **one** flag A/B experiment + **one** cost-per-active-user
  panel. When those exist and the join demo works, PostHog is done. No second funnel, no
  cohort/retention analysis.

**Shape-only event schema** (all server-side; `request_id` on every event; `distinct_id` = session
persona id, real = Danny, synthetic = generated persona, no PII):

| Event | Properties |
|---|---|
| `session_started` | `request_id`, `distinct_id`, `model_id`, `flag_variants` |
| `prompt_submitted` | `request_id`, `prompt_tokens`, `prefix_hash`, `model_id` |
| `first_token` | `request_id`, `ttft_ms` |
| `generation_completed` | `request_id`, `completion_tokens`, `tpot_ms`, `cost_usd`, `goodput_met` |
| `generation_failed` | `request_id`, `reason` (spot-evict / OOM / timeout) |

- **Funnel:** `session_started → prompt_submitted → first_token → generation_completed`; drop-off
  → Sentry trace by `request_id`.
- **Cost / active user:** sum `cost_usd` by `distinct_id`; `cost_usd` derives from the L0 cost
  post-processor (tokens × $/1M) — no new cost math.
- **A/B:** `flag_variants` as event property → compare `goodput_met` / `cost_usd` / `tpot_ms`.
  Claim a win only at **≥5 pp absolute on `goodput_met`** (a rate — α 0.05, power 0.80, a
  few-hundred synthetic sessions/arm) **or ≥10% relative on p95 latency / cost with
  non-overlapping CIs across ≥5 replays** (these ride replayed traffic, so they are measured with
  a confidence interval, not power-tested; 10% clears spot/thermal/batching jitter). Thresholds
  are fixed before the run — the constraint is honesty about the bar, never sample count.

---

## 6. Motivation hooks (pick two)

Two of the brief's four hooks, chosen by one lens: **pick hooks that ride a committed layer, not
ones that open a new workstream.**

- **Cost scoreboard** — live self-hosted $/1M-tokens vs commercial API cost. Rides the L0 cost/1M
  post-processor already committed in the harness; spans every layer; the most legible motivator.
- **Chaos day** — evict spot nodes mid-generation, capture a token stream surviving node death.
  Rides L2b spot-eviction survival (the heaviest layer); it is the *demonstration* of L2, not a
  new layer.

**Rejected** (not out of scope — just unselected): **speculative decoding** (new L1 draft-model
workstream + VRAM pressure on the 24 GB A10G) and **predict-before-measure** (new analytical
model, highest rabbit-hole risk). Both open new workstreams the binding constraint warns against.

---

## 7. Definition of done

The output is public evidence, not the cluster:

- Public repo, real commit history, Terraform-first, `just up` / `just down`.
- Write-ups with real charts, produced as phase deliverables: **L2b** ("…what breaks"), **L3**
  ("prefix-cache-aware routing vs round-robin"), **L4** (P/D disaggregation result). The cost
  scoreboard is a live panel across all phases.
- Each phase reachable is independently shippable at its definition of done above.

---

## 8. Non-goals / out of scope

- Fine-tuning or LLM training (training *infrastructure* experience exists; not repeated here).
- RAG, vector databases, embedding pipelines.
- Agent frameworks / orchestration.
- Ray Train (Ray Serve is in scope as a considered-and-rejected L2–L4 substrate).
- Multi-cloud or portability abstractions.
- Building a real product — this is a platform demonstrator.
- A self-hosted coding tool (a larger MoE coding model on a 48 GB card) — a *product* destination,
  not this demonstrator; parked as a possible later effort.

---

## 9. Architecture at a glance

```
                 ┌─────────────────────────────────────────────┐
   thin client   │                 EKS cluster                 │
  (Streamlit) ──▶│  IGW gateway ─▶ llm-d Router/EPP ─▶ vLLM     │
                 │      │            (prefix-cache-aware)  pods  │
                 │      │                       │  (Qwen3-8B     │
                 │      │                       │   AWQ-INT4,    │
                 │      │                       │   FP8 KV,      │
                 │      │                       │   LMCache)     │
                 │      │                                        │
                 │  request_id ──▶ OTel Collector ──┬─▶ Sentry   │
                 │                                  ├─▶ PostHog  │
                 │                                  ├─▶ Prom/Grafana
                 │                                  └─▶ Loki     │
                 │                                             │ │
                 │  Karpenter (spot nodes) + KEDA/HPA ─────────┘ │
                 │  (scale-to-zero, autoscale on num_requests_waiting)
                 └─────────────────────────────────────────────┘
   Terraform provisions all of the above.  just up / just down.
```

---

## 10. Open build-time decisions

Deliberately deferred — not needed to start Layer 0:

- **Deploy substrate (is Argo CD the CD tool?).** The brief assumes Argo CD; nothing locks it. The
  deploy→Sentry release marker rides whatever this resolves to. An **L2a** decision (Argo CD vs
  CI-driven Helm vs Terraform).
- **Per-phase experiment design detail.** The exact knob-sweep grid (L1), cold-start experiment
  design (L2a), routing eval design (L3), and P/D topology params (L4) are decided as each phase is
  reached. The phase table in §4 specifies each to the resolution this planning spec needs.
- **Version pins** for the pre-1.0 llm-d Router/EPP and IGW sub-APIs (`InferenceObjective`,
  adapter-rollout) — confirm against the releases pages at deploy time; they are fast-moving.

---

## 11. Decision log

Every decision above traces to a resolved wayfinder ticket. Full detail lives in the ticket; the
map is [Map: slipstream platform spec](https://github.com/pvardanis/slipstream/issues/1).

| Decision | Ticket |
|---|---|
| GPU / model / feasibility research | [#2](https://github.com/pvardanis/slipstream/issues/2) · findings `research/gpu-model-feasibility` |
| Serving substrate research | [#3](https://github.com/pvardanis/slipstream/issues/3) · findings `research/serving-substrate` |
| Inference-routing research | [#4](https://github.com/pvardanis/slipstream/issues/4) · findings `research/inference-routing` |
| Benchmark-harness scope (L0) | [#5](https://github.com/pvardanis/slipstream/issues/5) |
| PostHog role + thin chat client | [#6](https://github.com/pvardanis/slipstream/issues/6) |
| Motivation hooks (pick 2) | [#7](https://github.com/pvardanis/slipstream/issues/7) |
| Observability integration depth | [#8](https://github.com/pvardanis/slipstream/issues/8) |
| Lock GPU, model, serving substrate | [#9](https://github.com/pvardanis/slipstream/issues/9) |
| Phase sequencing, per-layer DoD, cut markers | [#10](https://github.com/pvardanis/slipstream/issues/10) |
| Spec assembly (this document) | [#11](https://github.com/pvardanis/slipstream/issues/11) |
