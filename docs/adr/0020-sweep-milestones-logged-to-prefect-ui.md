<!-- ADR recording why the knob-sweep logs its coarse milestones (redeploy start/finish, scraped ceiling, cell start/done+gate, point done, render published, run start/summary) so they surface on the Prefect run page and in worker stdout: new milestone lines at the flow/task level go through Prefect's `get_run_logger`, which attaches flow-run/task-run context and routes each line to the right UI page; the Prefect-free sweep core keeps its stdlib `logging.getLogger(__name__)` calls and is pulled into the UI by `PREFECT_LOGGING_EXTRA_LOGGERS=slipstream_bench` baked into the orchestration image (the handler) plus the flow's `enable_milestone_logging` lifting the `slipstream_bench` logger to INFO (the level the env var does not set), because the core runs inside the active run context so Prefect's log interception tags those records with the right run for free — no logger injected through the core, so the Prefect-free boundary (test_sweep_is_prefect_free) holds. Levels: INFO=milestone, WARNING=degraded-but-continuing, ERROR=abort. run_id rides only the run-start and final-summary lines; the UI groups the rest by run and subflow (point slug). Extends ADR-0018 (render to UI), depends on the Prefect-free core of ADR-0012/0015. -->

# ADR-0020: The knob sweep logs its milestones to the Prefect UI

- Status: Accepted
- Date: 2026-10-08

## Context

The knob-sweep flow (ADR-0015) runs unattended on an in-cluster process worker, and ADR-0018
put its *results* on the run page. Its *progress* is still close to invisible: the orchestration
code logs through the Python stdlib (`_LOGGER = logging.getLogger(__name__)`) at INFO/WARNING in
three modules (`flows/knob_sweep.py`, `tasks/cell.py`, `completion.py`), there is no
`get_run_logger` anywhere, and no Prefect logging config. Inspecting a running sweep via the
worker shows minimal output, and the long, control-flow-deciding steps — the ~20-minute vLLM
redeploy per engine point, the scraped concurrency ceiling, which cells ran versus resumed from
cache — pass silently. The felt need is to **see the important sweep updates on the Prefect run
page and in worker stdout**, without turning either into noise.

Two Prefect v3 facts shape the options:

- `get_run_logger()` returns a logger already bound to the active flow-run/task-run context, so its
  lines land on the correct run page. It only works *inside* a flow or task runtime — it cannot be
  called from code that also runs outside one.
- Prefect's log interception only captures loggers it is told about. A module logger like
  `slipstream_bench.orchestration.*` does **not** reach the UI unless its name (or a parent) is in
  `PREFECT_LOGGING_EXTRA_LOGGERS`. This is why the existing stdlib lines are effectively invisible in
  the UI. When such a logger emits while a run context is active, the interception tags the record
  with that active run — so captured core logs land on the right page with no context passed by hand.
- `PREFECT_LOGGING_EXTRA_LOGGERS` attaches the API log handler to the named logger but sets **no
  level** on it. The logger inherits root's WARNING default, so INFO milestones are filtered before
  the handler ever sees them — the env var alone surfaces nothing. The level must be lifted to INFO
  explicitly, and since no Prefect env var sets a per-logger level, that lift is a line of code.

The hard constraint is the Prefect-free sweep core: `drive_knob_sweep`, `_drive_point_sweep`, and
`run_cell` carry the per-cell/per-point milestones but must not import Prefect
(`tests/orchestration/test_sweep_is_prefect_free.py` enforces it, per ADR-0012/0015). So a
milestone that fires inside the core cannot call `get_run_logger`.

## Decision

### Two emitters, split by layer

- **Flow/task level** (run start, run summary, redeploy start/finish, render published) gets new
  lines through `get_run_logger()`. These already live in Prefect-coupled code, and the bound
  context puts them on the right page.
- **The Prefect-free core** keeps its stdlib `_LOGGER` and gains milestone lines the same way. They
  reach the UI because the image sets `PREFECT_LOGGING_EXTRA_LOGGERS=slipstream_bench` (the handler)
  and the flow lifts the `slipstream_bench` logger to INFO at its composition root (the level) via
  `enable_milestone_logging`; they carry the correct run context automatically since the core
  executes inside the active flow/task runtime. The lift lives in the Prefect-coupled flow layer,
  not the core, so the Prefect-free boundary is untouched.

No logger or sink is threaded through the core's signatures: the core does not learn it is running
under Prefect, so the Prefect-free boundary and its test hold unchanged. This is the deciding reason
to prefer extra-loggers over injecting an `Echo`-style sink (the executor's pattern, ADR-0017) down
the core call chain.

### Which milestones

The long or control-flow-deciding steps, at INFO: engine-point **redeploy start and finish**, the
**scraped concurrency ceiling** value, per-cell **start** and **done with its SLO-gate result**
(ADR-0019), **point done** (its cell count and whether it ran or resumed), **render published**,
**run start**, and a **final summary** (cells run vs resumed). Fine-grained inner steps — the
one-time mTLS proxy-up, per-request detail — stay off the milestone stream.

A per-cell *cached-skip* line is deliberately **not** emitted: a Prefect cache hit skips the cell
task body entirely (ADR-0012), so the Prefect-free core never runs to log it. The resume signal
surfaces once per point instead — the **point done** line reports the point as resumed — which is
the granularity an operator reads progress at. The run/resume split in the summary is likewise
point-granular: a point with any pending cell counts all its cells as run, so `cells_run` is an
upper bound on cells actually executed when a point is partially cached, not a per-cell count.

### Levels

INFO = milestone, WARNING = degraded but continuing (a retry, the best-effort artifact-publish
failure already logged at WARNING), ERROR = an abort (empty ceiling, failed cell). An abort is not a
hand-written ERROR line: the empty-ceiling `CeilingScrapeError` and a failed cell both raise out of
the flow/task, and Prefect records the failed run/task state — the orchestration code emits no
`_LOGGER.error`/`.exception` of its own, so the ERROR surface is Prefect's run-state logging, not a
milestone this code writes. This keeps the UI's level filter meaningful without duplicating Prefect's
own failure records.

### run_id on records

`run_id` is domain/path data, not a log field (it tags the parent run `run=<run-id>` and names each
subflow by point slug). It rides only the **run-start** and **final-summary** lines; every other
line relies on the UI grouping by run and subflow. No `contextvars`/`bind` machinery.

### Enablement travels with the image

`ENV PREFECT_LOGGING_EXTRA_LOGGERS=slipstream_bench` is set in the orchestration Dockerfile, so both
the in-cluster worker and a manual `slipstream-orchestrate knob-sweep` run off the same image attach
the API log handler to the package logger — independent of work-pool or Prefect profile config. The
handler is only half of it: the flow's `enable_milestone_logging` lifts the logger to INFO so the
core lines clear the inherited WARNING threshold and reach that handler. Both travel with the flow,
so every launch path captures the core logs.

### Rejected alternatives

- **Inject a logger/sink through the core** (mirror the executor `Echo` sinks). Rejected: it adds a
  parameter down every core call for no UI gain over extra-loggers, and it pushes a logging concern
  into the Prefect-free core that the boundary test exists to keep out.
- **Set `PREFECT_LOGGING_EXTRA_LOGGERS` only on the worker Helm release** (`terraform/eks/worker.tf`).
  Rejected: it would miss manual CLI runs off the same image; baking it into the image covers every
  way the flow is launched with one line.
- **Bind `run_id`/point slug onto every record** via `contextvars`. Rejected as redundant with the
  UI's run grouping and subflow naming, for machinery the harness does not otherwise need.

## Consequences

- The orchestration flow/task modules gain `get_run_logger` call sites; the Prefect-free core gains
  stdlib milestone lines only — no new imports, no signature changes, the Prefect-free test unchanged.
- The orchestration image carries `PREFECT_LOGGING_EXTRA_LOGGERS=slipstream_bench` and the flow
  lifts the `slipstream_bench` logger to INFO; any future `slipstream_bench.*` logger is captured
  into the UI by the same handler and level.
- Milestones are testable without mocks: stdlib lines via `caplog`, and the one line with logic —
  the final summary's run/resume counts — via a small pure message-builder tested directly.
- Ships as code (the call sites) plus one build change (the Dockerfile `ENV`); the two are coupled
  (neither reaches the UI without the other), so they land together.
