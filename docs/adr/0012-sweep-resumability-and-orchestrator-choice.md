<!-- ADR recording why the knob/load sweep gains resumability through config-digested per-cell caching, why the sweep is a joinless chain rather than a DAG problem, why Prefect is adopted as a cache/retry/observability layer (not as a scheduler), and why its server runs locally against S3-backed state. -->

# ADR-0012: Sweep resumability and the orchestrator choice

- Status: Accepted
- Date: 2026-09-23

## Context

The knob sweep (ADR-0009) and the standalone load sweep run as an imperative bash loop
in `just knob-sweep`: ~20 Tier-1 engine points, one GPU redeploy each, a Tier-2 client
ladder per point, everything sequential. A sweep is hours long on one A10G replica. When
a single cell fails — a specific engine point under a specific `--max-concurrency` rung —
the loop keeps its partial results (`overall=1`) but there is no way to **resume**: the
next invocation reruns every cell, including the ones already measured. The felt need is
resumability at cell granularity, and a way to know a result is stale when the config that
produced it changed.

That need reads, at first, like a call for a DAG engine. It is not. The sweep's dependency
graph is a **joinless chain**: the ~20 engine points are **data-independent** — point 7 does
not consume point 6's output — and within a point the client cells fan out with **no fan-in**.
The only thing ordering the points is the single GPU: a **semaphore(1)**, a resource mutex,
not a data edge. A DAG *executor* exists to schedule a rich data-edge structure (branches,
diamonds, fan-in joins); this graph has almost none. A `for` loop expresses it with zero loss.
So the question is not "chain vs DAG" (a chain is a degenerate DAG) but "does the shape need a
DAG *engine*" — and it does not.

The knowledge base names no specific orchestrator (Prefect, Dagster, Temporal all absent;
Airflow and Argo appear only as examples), but covers the concepts generically under "workflow
engine" — `distributed-ml-patterns.md` §4 (fan-out/fan-in topology, step memoization) and §5
(retry classification, metadata/observability). It backs the decisions below directly: §4 lists
fan-in/fan-out DAG topology under "when NOT to use" for sequential dependencies and warns DAGs
"add complexity to reason about, debug, visualize"; it frames step memoization as content-hashing
inputs to skip unchanged steps (this ADR's digest cache); and it repeatedly notes always-on
replicas cost money when idle, favouring event-driven over idle long-running services (this ADR's
case against an always-on on-cluster server). It flags cache-invalidation and cache-entry GC as
real ongoing ops work. It has no Prefect-specific, EKS-cost, or "self-host control plane vs local"
page. The Prefect facts below — `cache_key_fn`, separate `key_storage`, the SQLite-default
`prefect server start`, and the Postgres+Redis self-host stack — were confirmed against current
Prefect v3 docs (caching concepts, self-hosted docker-compose / Helm).

## Decision

### Resume is per-cell, keyed on a deep config digest, gated on a valid result

The unit of resume is the **cell** (engine point × client-load coordinates), matching where
failures actually land: a point deploys fine, then a rung errors mid-ladder. A cell is
skipped only when its result is **done** — defined as **the cell JSON exists, parses, carries
the expected result fields, and passes a semantic sanity check** (non-zero completed requests and
an error rate under threshold), reusing the `aggregate-sweep` parser as the predicate. Neither
file presence nor mere parseability is trusted: the gate must reject not only a half-written or
errored JSON but a structurally-complete result from an unhealthy server — every request errored,
goodput 0 — which parses and carries all fields yet is not a measurement. Under sample-is-canon
such a degenerate cell would otherwise freeze into the campaign until a manual resweep, so the
sanity check raises and re-attempts it.

"Stale" is defined by a **deep config digest** — `sha256(model.yaml + sweep-grid.yaml + vLLM image ref)`. The
`run_id` timestamp (ADR-0009) versions a *run* but not the identity of the config that produced
a cell: two runs with the same engine-point slug but a different model or grid are otherwise
indistinguishable. The digest closes that hole. Engine-point and client-load coordinates are
already addressed by the slug and cell filename, so the digest must cover model + grid + serving
image. `model.yaml` pins the model identity (`hfId`, the Hugging Face `revision` commit,
`quantization`, `kvCacheDtype`), so a re-quantized checkpoint already moves the digest — but it
does **not** pin the vLLM serving image, which `k8s/vllm-gpu.yaml` holds by convention. The
vLLM image ref must therefore be folded into the digest inputs explicitly: a vLLM bump changes
the numbers and must invalidate the cache. Code-version hashing of
the cell-command itself is **out of scope**: grids are frozen for a run, and mid-campaign
cell-logic edits are not a workflow we support; that is the boundary at which a content-addressed
build system would be reconsidered.

### A benchmark cell is a single sample, taken as canon

`vllm bench serve` is not a pure function: the same engine args under the same client load
yield different numbers run to run (goodput noise, scheduler jitter). Content caching assumes
same-inputs ⇒ same-output, which a benchmark violates. The decision is **sample-is-canon**:
cost (one GPU) forces one measurement per cell, and a cached cell is reused until an *input*
changes. Each cell result records that it is a single sample, so a cached number is never
mistaken for a converged mean. Re-running identical cells to average out noise is a deliberate
manual act (delete-and-resweep), not the default.

### Prefect is adopted as a cache/retry/observability layer, not as a scheduler

Because the graph is a joinless chain under a semaphore(1), a DAG engine's scheduling buys
nothing. What is wanted are three orthogonal features: a **content-addressed skip** (resume),
**opt-in retries** on flaky rungs, and a **live view** of an hours-long unattended run.
Prefect v3 supplies exactly these via `@task`, and its graph/scheduler machinery goes unused —
that is accepted with eyes open. This is not a claim that the sweep is a DAG; it is a chain that
borrows two decorators and a UI.

- **Skip** = `cache_key_fn` returning `digest:point-slug:cell-name`. On a hit the task enters
  `Cached` and does not run.
- **Validity gate** = the task raises on an unparseable result, so a failure is never cached and
  is re-attempted on the next run.
- **Retries** = `@task(retries=…)`, opt-in per invocation.

The lighter alternatives were weighed. A dependency-free digest+skip in `sweep/` is a day of
testable Python but has no retries and no UI. `doit`'s `uptodate` predicate is the purpose-built
"skip done work" wheel without a server, but likewise no retries and no view. Prefect earns the
extra dependency **only** on the strength of retries + observability, both independently wanted;
absent those two, `doit` or DIY would win.

### The per-cell JSON in S3 is the single source of truth

There is exactly one copy of a measurement: the per-cell JSON the bench task writes to S3.
Prefect does not hold a competing copy. Its `key_storage` is configured **separately** from
result storage, the task **returns a pointer** (the cell's S3 path), and the in-task validity
check against that JSON is authoritative — if Prefect's cache ever disagrees with the JSON, the
JSON wins. This yields two distinct, non-duplicating S3 prefixes: the cell JSON (the numbers)
and Prefect's cache/key index (pointers + metadata).

This split makes the local server disposable: **`result_storage` and `key_storage` must be
explicitly pointed at S3** (Prefect's default is local `~/.prefect/storage/`). With them on S3,
resume survives any server or laptop death — the cache lives in S3, not in the server's database.

One caveat this durability rests on: Prefect persists a task's result and cache record at
**transaction commit time**, and each `@task` is its own transaction by default, so each cell
commits to S3 on completion — which is what lets an interrupted run keep its finished cells. The
sweep must therefore **not** be wrapped in an enclosing `transaction()`: that would defer every
write to flow end and forfeit mid-run resume. Per-cell durability is the default, not an
unconditional guarantee.

### The Prefect server runs locally, against S3-backed state

Self-hosting Prefect on the EKS cluster is a stateful stack — Postgres + Redis + server +
background services (Helm `prefect-server` / `prefect-worker`). That is an always-on control
plane with a database, recurring cost for an intermittently used tool, and it fights the founding
constraint that made this a singleton-GPU problem (ADR-0006: cost forced one on-demand replica,
torn down between sweeps). It is declined.

The server (API/UI/DB) is separate from where flows run; the sweep runs from the workstation
driving `kubectl` + SSM, as it does today. So the server runs **locally during sweeps** —
`prefect server start`, SQLite `~/.prefect/prefect.db` — with the UI up exactly when a sweep is
being watched, at zero cloud cost. SQLite holds only run history and the UI timeline; losing it
loses the timeline, never the cache or results (both S3). Its single-writer limit is a non-issue
for a serial, low-task-rate sweep.

### Scope boundaries and reopen triggers

- **Logic lives in Python `sweep/`, the justfile stays thin.** The digest, the parse-validate
  gate, and the skip decision are testable Python shared by both `knob-sweep` and the standalone
  load sweep; the recipe calls into them rather than grepping JSON in bash.
- Revisit the orchestrator choice when any of: a **GPU fleet** makes points genuinely parallel
  (real edges to schedule); **mid-campaign cell-logic edits** become a workflow (code-version
  invalidation); or **unattended multi-hour/multi-day sweeps** make "close the laptop and walk
  away" a real pain — the fix there is promoting the **flow-runner** (a Prefect worker) to the
  always-up bench-endpoint host or EKS, at which point a remote server naturally follows.

## Consequences

- This ADR is prose only; no sweep code changes here. Implementation ships as a later stack, one
  kind of change each: the Prefect dependency (deps), the digest + validity-gate + `cache_key_fn`
  task in `sweep/` (code), the recipe wiring `knob-sweep` and the load sweep through it
  (orchestration).
- A Prefect dependency enters the bench package, joining `typer`, `pydantic`, `prometheus-client`,
  and the plotting stack.
- Resume correctness hinges on one configuration fact: `result_storage` and `key_storage` must be
  S3, not Prefect's local default. That is called out here because it is the hinge of the design,
  not an implementation detail.
- The digest inputs (`model.yaml` + `sweep-grid.yaml` + the vLLM serving image ref from
  `k8s/vllm-gpu.yaml`) become a cache-invalidation contract: any input that changes a cell's
  meaning must be inside the digest, or a stale cell is silently reused. `model.yaml` already
  pins the model revision and quantization; the serving image is the one meaning-bearing input
  living outside it and must be added by hand.
- The UI is local and solo — no shared run links, no history beyond the local SQLite file. That is
  accepted for a single-operator harness and is the first thing the reopen triggers above would
  change.
