<!-- ADR recording why the knob-sweep result views gain the two p95 SLO gates (ttft prefill-bound, tpot decode-bound) and the output token rate on every rung and ceiling row, so the cliff says which gate bit and at what capacity — not just that a rung fell; why these three metrics are required fields on the load cell parsed fail-fast, not optional; why get_ceiling returns the whole winning cell (so the ceiling row reads that rung's own gates at the capacity edge, not a fold across the ladder); why the cliff plot annotates only the ceiling rung and the fallen rungs (the flat passing band reads the same gates and would wall the facet); and why each cell task publishes its pointer and four metrics as an unkeyed markdown artifact from inside the task body — the only context that attaches it to the sub-task itself. Builds on ADR-0009 (ceiling method) and ADR-0018 (render to Prefect UI). -->

# ADR-0019: The SLO gates and token rate per rung, and per-cell result artifacts

- Status: Accepted
- Date: 2026-10-07

## Context

The knob sweep's result views (ADR-0018) answer *how much* concurrency each engine
config held — the ceiling table and plot — and *where the ladder fell* — the goodput
cliff. Both read only one number per rung: the goodput fraction. The cliff says a rung
fell below the 0.95 floor but not **which of the two SLO gates bit**. `goodput_fraction`
is defined as the fraction of completed requests meeting *both* p95 thresholds — ttft
(time to first token, prefill-bound) and tpot (time per output token, decode-bound) —
so a bare fraction collapses the one diagnostic that tells an operator which engine knob
to turn: a ttft fall is prefill pressure, a tpot fall is decode pressure. The sweep also
parses `request_throughput` already (it is the denominator of the goodput fraction), so
the capacity at each rung — output tokens/s — is a near-free read the views drop.

Separately, each cell (one `vllm bench serve` rung) runs as its own Prefect `@task`
(ADR-0012) that returns an S3 pointer and caches on `digest:point-slug:cell-name`. The
grid-level render (ADR-0018) folds the whole run onto the *parent* run page, but a single
cell's own result — its pointer and its numbers — is nowhere on the **sub-task** in the
UI; an operator reading one cell's run reads a bare pointer string.

Prefect artifact facts (confirmed against Prefect v3.8 docs): `create_markdown_artifact`
binds the artifact to the **active run context** — the task run if called inside a task
body, else the flow run — and takes no run-id parameter, so there is no way to attach an
artifact to a task run from outside that task's body. State-change hooks
(`on_completion`, etc.) run **outside** the task-run context. A cache hit puts the task
run in a `Cached` state and **does not execute the body**.

## Decision

### The two SLO gates and the token rate are carried on the load cell

`LoadCell` (the contract parse kernel, ADR-0010) gains `p95_ttft_ms`, `p95_tpot_ms`, and
`output_throughput`, parsed from the cell JSON alongside the fields that already feed the
goodput fraction. They are **required** fields, parsed fail-fast through the same
`to_numeric_metric` guard as the rest — a missing, null, non-finite, or negative value
raises `SweepAggregationError` and the cell is not a measurement. The gates and the rate
are present in every real `vllm bench serve` result; treating them as optional would let
a malformed result parse as a half-measurement and surface as blank cells in the table,
hiding a broken run behind a plausible row. Fail-fast keeps a cell either a full
measurement or a loud rejection (consistent with the validity gate, ADR-0012).

### `get_ceiling` returns the winning cell, and both tables carry the three metrics

The ceiling row reads the three metrics **at the ceiling rung itself** — the highest
passing rung, the capacity edge — not folded or averaged across the ladder. The gates at
the edge are the ones that describe the config's sustained operating point; an average
across rungs would blend the easy low-concurrency rungs into the number that is supposed
to describe the limit. So `get_ceiling` widens from returning the ceiling's
`max_concurrency` to returning the whole `LoadCell`, and the fold reads that cell's own
gates and rate (or `None` alongside a `None` ceiling, when no rung held — never an
invented zero). Both views carry the three columns: the **ceiling table** (the headline
per config) and the **cliff table** (the per-rung diagnostic).

### The cliff plot annotates only the ceiling rung and the fallen rungs

The goodput-cliff plot annotates each marker with its three metrics, but only where they
diagnose something: within each prefix-share line, the **ceiling rung** (the capacity
edge) and **every rung that fell** below the floor. The passing rungs below the ceiling
sit on the flat top of the cliff reading near-identical gates, so labeling them walls the
facet in a band of redundant numbers (confirmed by eyeballing a dense 8-facet render).
The labels left are exactly the ones an operator reads to diagnose a fall: the edge, and
which gate bit past it. Units are named once in the figure subtitle, so each marker
carries only numbers.

### Each cell publishes its pointer and metrics as a sub-task artifact

`run_cell`, once a result passes the validity gate, re-reads the cell and publishes an
**unkeyed** markdown artifact — the S3 pointer and a one-row table of the four headline
metrics (goodput fraction, the two gates, the token rate) — onto the running task. The
publisher is an injected collaborator, so the Prefect-free core is covered against a
fake; the production task binds `create_markdown_artifact`.

The artifact is published **from inside the task body**. This is forced, not chosen:
`create_markdown_artifact` binds to the active run context and cannot target another run,
and state-change hooks run outside the task-run context — so the body is the only place
the artifact can land on the sub-task itself (the literal ask). Unkeyed, it shows in that
task run's own Artifacts tab rather than a cross-run timeline, which fits a per-cell
result that is not compared across runs.

The consequence of the body-only seam: a **cache hit skips the body**, so a reused cell
draws no new artifact on its cached task run. This is accepted — a cache hit means the
cell was already measured and its measuring run already carries the artifact; a hit
produces no new numbers to show.

The publish is **best-effort**: a publish failure (a transient Prefect API error) is
logged with the cell's pointer and swallowed, not raised. The measurement is already
durable in S3 by the time the artifact is written, and the artifact is a UI convenience,
so raising would leave the task uncached and force a re-run of the expensive GPU
benchmark — coupling an expensive measurement's durability to a cosmetic write, against
the ADR-0012 resume intent. The validity gate's own raise stays fatal, because the two
failures differ in kind: an invalid result *should* void the cache and re-run; a failed
UI write should not.

### Rejected alternatives

- **The gates and rate as optional fields.** Parse them when present, leave blank when
  absent. Rejected: they are present in every real result, so an absence is a malformed
  run, and a blank row hides it behind a plausible-looking table.
- **The ceiling row folds the gates across the ladder** (mean/max over rungs). Rejected:
  the ceiling is a single rung — its edge gates describe the operating limit; a fold
  blends in the slack of the easy rungs.
- **Annotate every rung on the cliff plot.** Rejected after eyeballing: the flat passing
  band becomes an unreadable wall of near-identical numbers; the diagnostic lives at the
  edge and on the fall.
- **Publish the per-cell artifact from a state-change hook or a wrapper, so it survives a
  cache hit.** Rejected because it does not meet the requirement: a hook runs outside the
  task-run context and the artifact API cannot target the sub-task from there, so the
  artifact would not land "on the sub-task" at all. The body is the only seam that does,
  and the cache-hit gap it carries is acceptable.
- **Let a publish failure raise (fatal publish).** Rejected: the measurement is durable
  in S3 before the artifact is written, so raising would void the cache and re-run an
  expensive GPU benchmark for a failed cosmetic write. Best-effort (log and continue) is
  consistent with the artifact being non-critical.

## Consequences

- `LoadCell` rejects any cell JSON missing the two gates or the throughput — a stricter
  parse than before. Every fixture and real result must carry all three.
- `get_ceiling` returns `LoadCell | None` rather than `int | None`; the ceiling fold and
  its callers read the cell.
- The per-cell artifact appears only on the task run that measured the cell. A fully
  resumed sweep (every cell cached) shows no per-cell artifacts on the resumed run's
  sub-tasks — the artifacts live on the sub-tasks of the run that first measured them.
- The cost baseline and the grid-level render (ADR-0018) are unchanged; this ADR adds
  columns to the existing views and one artifact to the existing cell task.
