<!-- ADR recording the knob-sweep rig: how the L1 concurrency ceiling is defined, which engine knobs are swept, how the rig varies them and cohorts failures, and what the chart reports (spec §3.1, epic #19 / issue #33). -->

# ADR-0009: Knob-sweep rig and the concurrency-ceiling method

- Status: Accepted
- Date: 2026-09-21

## Context

ADR-0006 brought up the GPU replica with **un-tuned** engine args (`--max-num-seqs 16`,
`--gpu-memory-utilization 0.90`, `--max-model-len 4096`) and said so in the manifest: the
concurrency ceiling is found by the knob sweep in #33, not chosen by hand. This ADR records
that sweep — what it measures, how, and what it deliberately leaves to later layers.

The L0 harness (ADR-0003) already sweeps **client-side** workload — it shells out to
`vllm bench serve` once per grid cell over `request_rate × prefix_share × burstiness` and
post-processes the result JSON (`cost`, `prefix-cache`, `report` subcommands). It has **no**
notion of engine knobs: the three tuning args are literals in `k8s/vllm-gpu.yaml`, and the
harness never varies them. #33 adds the missing **server-side** sweep on top of that harness.

The knowledge base (`vllm-internals`) covers chunked prefill, prefix caching, the
PagedAttention block math, and the SLO metric definitions directly; it does not cover
`max-num-seqs` or FP8-KV by name. Those two, and the engine-arg ranges below, were confirmed
against current upstream vLLM docs (`arg_utils`, benchmarking CLI).

## Decision

### The ceiling is closed-loop concurrency at a goodput SLO

"Max sustained concurrency at SLO" is defined as the **highest `vllm bench serve
--max-concurrency` value** (a client-side semaphore capping in-flight requests) at which
**goodput ≥ 95%** of requests — goodput being requests meeting **both** `ttft:1000` and
`tpot:50` (ms). Those thresholds are reused verbatim from `cli_helpers.py`
(`DEFAULT_GOODPUT`, matching spec §L0), a single source shared with every other SLO judgment
so an offline sweep number and a future live Grafana panel agree by construction.

Closed-loop (`--max-concurrency`, fixed in-flight count) is chosen over open-loop
(`request_rate`, fixed arrival rate — what the harness sweeps today) because the deliverable is
a **count of simultaneous requests**, not an arrival rate. Under a fixed arrival rate the
in-flight count floats and unbounded queueing contaminates the SLO cliff — the number read
back reflects queue depth, not replica capacity. The semaphore pairs 1:1 with the server's
`max-num-seqs`: offered concurrency N and the engine's batch cap are the same axis, read from
both sides.

### Two-tier swept grid, pruned not cartesian

The four knobs the ticket names are not co-equal. Following the KB's roofline framing — locate
the saturation batch first, then vary interacting knobs only near it — the grid is two tiers:

**Tier 1 (engine args, one manifest redeploy per point):**

- `max-num-seqs` ∈ {16, 32, 64, 128, 256} × `kv-cache-dtype` ∈ {fp16, fp8} — the interacting
  KV-capacity pair. FP8 roughly doubles KV slots, shifting where `max-num-seqs` saturates, so
  these are swept jointly and the clamp point read off. FP8 is the rig's committed KV dtype
  (spec §3.1, ADR-0006); **fp16 is swept as the counterfactual baseline, not a candidate** — it
  quantifies what FP8 buys in isolation, it is not a config the deliverable might select.
- `enable-prefix-caching` ∈ {off, on} — treated as a **condition, not a crossed axis**: *off*
  gives the true-capacity ceiling (prefix caching inflates measured concurrency whenever bench
  prompts share a prefix, since cached blocks consume zero new KV), *on* is the prod-realism
  pass.

**Tier 2 (client workload, per Tier-1 point, no redeploy):**

- `--max-concurrency` ladder {8, 16, 32, 64, 128, 256} — the ceiling search itself, raised until
  goodput drops below 95%.
- `prefix_share` — swept over {10, 50, 90} only when `prefix-caching=on`; when off it is pinned
  to a single `0` baseline. With caching off vLLM reuses no prefix KV regardless of how much
  prefix requests share (`KVCacheManager.get_computed_blocks` returns zero computed blocks when
  `enable_caching` is false, vLLM v0.29.0), and the workload holds `total_len` fixed while share
  only re-partitions it into prefix/suffix, so goodput is flat across share — a swept share there
  measures a definitional null. The ladder is therefore {10, 50, 90} under caching-on and {0}
  under caching-off, so no null cell is ever run.

**Held fixed:** `max-model-len=4096` (a product constraint, not a perf knob — halving it would
double the ceiling by serving a shorter context, so its doubling relationship is noted here
rather than swept), vLLM's default `block-size` of 16 (sweeping it coarsens prefix-cache
granularity and muddies the concurrency signal — a separate micro-experiment if ever wanted),
`gpu-memory-utilization=0.90`,
chunked prefill at its default (only bites at long prompts; the workload's `total_len≈1000` is
short), `tensor-parallel-size=1`.

### Rig mechanics — `just knob-sweep` orchestrates, the CLI post-processes

There is no engine-knob parametrisation today. The Tier-1 loop lives in a new `just knob-sweep`
recipe (orchestration): render the manifest with the point's knob values (envsubst, matching the
existing `gpu-node-pool.yaml` convention), `gpu-deploy`, wait for rollout, scrape vLLM's startup
`Maximum concurrency for N tokens per request` line as a *predicted* ceiling, then invoke the
existing `bench` path for the Tier-2 client ladder, and collect. The Python CLI stays pure
post-processing over collected JSON. Results land under `bench/results/<run_id>/`, one subdir per
engine config (`mns{N}_kv{dtype}_pc{on|off}/`).

Manifest templating is envsubst now; a Helm chart deriving from the model definition (anticipated
by ADR-0008) is a **separate** concern on its own ticket under epic #19 — when it lands, the sweep
swaps `envsubst` for `helm --set`. #33 does not drag that migration onto its critical path.

### Failure cohorts — {timeout, oom, other}, spot-evict dropped

`generation_failed` is cohorted **{timeout, oom, other}**. The ticket's original "spot-evict"
cohort is **dropped**: the bench rig runs **on-demand** (ADR-0006 chose on-demand over spot for
deterministic ~90s launch), so a spot eviction is not an observable failure here. Sources:
*timeout* from the client result JSON (request past deadline), *oom* from the pod `OOMKilled`
event plus an engine-log CUDA-OOM / KV-cache-full scrape, *other* from a non-zero exit with
neither signal. vLLM's preemption counter (`vllm:num_preemptions`) is surfaced separately as a
soft-fail signal — the real "`max-num-seqs` pushed too high" tell, distinct from a hard failure.

### Chart — offline artifact, data first

A new `chart` subcommand renders a static PNG (seaborn, atop matplotlib) to
`bench/results/<run_id>/charts/`, with the aggregated ceiling table written beside it as
CSV/JSON — the table is the durable artifact, the PNG disposable. Aggregation is a **new**
`sweep_aggregation.py` module (CLI `aggregate-sweep`) keyed by (`max-num-seqs`, `kv-cache-dtype`,
`prefix-caching`), reusing `results.py` readers — not folded into `report.py`, whose job is the
single-config cost×prefix economics join, a different key and output.

- Primary chart: y = max sustained concurrency at SLO, x = `max-num-seqs`, series =
  `kv-cache-dtype`, faceted by prefix-caching × `prefix_share`. Caching-off carries the single
  `0` share, so its row is one facet (labelled share n/a) while caching-on spans the {10, 50, 90}
  share columns — a ragged grid, no duplicated null cells.
- Diagnostic: goodput vs `--max-concurrency` per config — the goodput cliff, showing *where* SLO
  breaks, not just the ceiling number.

### Scope boundaries

- **Single replica throughout.** Every number describes one A10G replica's capacity — the
  per-replica unit L2 scaling multiplies and L3 routing exploits. No multi-replica or routing
  axis enters this sweep; the aggregated ceiling table is emitted as clean keyed data so those
  later layers read it as input.
- **Offline batch artifact, not telemetry.** The chart is a decision document, not a Grafana
  panel. Grafana (epic #19) later reads live Prometheus — TTFT/TPOT/throughput/prefix-cache-hits
  directly, goodput as a derived PromQL panel over the *same* shared SLO thresholds.

## Consequences

- #33 ships as a small PR stack, one kind of change each: this ADR + glossary (docs), the
  envsubst templating + `just knob-sweep` recipe (orchestration), `sweep_aggregation.py` +
  failure classification (code), the seaborn + pandas plotting dependency (deps), the `chart`
  subcommand (code).
- The sweep is ~20 Tier-1 redeploys (~90s+ each) × a cheap Tier-2 client ladder. It runs in the
  label-gated cloud tier (ADR-0002) — it needs a live GPU — hand-triggered, never on push.
- Once the sweep produces real numbers, `k8s/vllm-gpu.yaml`'s placeholder engine args are
  patched with the tuned values. That is a data-driven config change on its **own** later commit,
  not part of building the rig — the rig has to run before the numbers exist.
- A first plotting dependency (seaborn + pandas, atop matplotlib) enters the bench package,
  joining `prometheus-client` + `typer`.
