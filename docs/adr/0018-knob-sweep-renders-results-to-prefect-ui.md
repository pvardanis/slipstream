<!-- ADR recording why the knob-sweep flow renders its grid-level result artifacts to the Prefect UI from a single terminal task that runs after the point loop: the two aggregators fold the whole run, so the ceiling and cliff views are cross-point and belong on the parent run page, not per subflow; the task is a @task (not inline like the config artifact) so a render failure retries idempotently off S3 without discarding the already-persisted cells; tables publish as durable markdown artifacts and plots as image artifacts via a presigned S3 URL (inline but expiring), with the PNG in S3 the durable copy. The cost baseline (report command) stays offline — the knob sweep does not run its arms. Depends on the report member of ADR-0017. -->

# ADR-0018: The knob sweep renders grid-level result artifacts to the Prefect UI

- Status: Accepted
- Date: 2026-10-05

## Context

The knob-sweep flow (ADR-0015) publishes exactly one Prefect artifact today: the
config-provenance markdown (`publish_config_artifact`, run inline in the flow body). It returns a
list of S3 cell pointers and aggregates or charts nothing. The result views that exist — the
ceiling and goodput-cliff tables and their plots — are produced only offline, by a human running the
`report`/`chart` CLI against a local run directory (ADR-0009). The felt need is to **see a sweep's
results on its Prefect run page**, so an unattended run is inspectable without a separate offline
step.

The reporting building blocks already exist and will live in the `report` member (ADR-0017):
`aggregate_ceilings` and `aggregate_rungs` fold a run directory of cell JSONs into ceiling rows
(one per engine-point × prefix-share) and rung rows (one per ladder rung), and `chart.py` renders
each as a Markdown table, a JSON table, and a PNG plot. The S3→local materialization the render
needs already exists in pattern: `orchestration/completion.py` downloads a point's cell objects from
S3 to a tempdir keyed exactly as the aggregators read them.

Prefect artifact facts (confirmed against current Prefect v3 docs): a **markdown** artifact embeds
its text and renders inline and durably in the UI; an **image** artifact renders a PNG inline but
takes a *reachable URL* — it does not embed bytes. The results bucket is private, so an image
artifact is fed a **presigned** S3 URL, which expires (SigV4, ~7 days max), after which the inline
image 404s. The common production pattern is to render matplotlib in a task, upload the PNG to
object storage, and reference it from an image artifact, keeping run-scoped visuals in the
orchestrator's UI; external dashboards (Grafana/MLflow) are the answer for scale, cross-run
aggregation, or non-Prefect audiences — none of which this single-operator harness has.

## Decision

### One terminal render task, under the parent flow, grid-level

After the parent `knob_sweep_flow` point loop completes, a single render step materializes the whole
run from S3, folds it with both aggregators, renders the tables and plots, uploads the PNGs, and
publishes the artifacts. It runs **once, at the end** — not incrementally per point.

Grid-level is forced by what the views mean, not chosen. `aggregate_ceilings` and `aggregate_rungs`
both read the whole run; the ceiling table and plot are inherently cross-point (the plot's x-axis is
`max_num_seqs` across points, its series `kv_cache_dtype` across points), so a single point is
degenerate. The cliff is per-point *data* but renders as one faceted figure (one facet per point).
All of it is a comparison across the grid, so the artifacts belong on the **parent run page**; the
per-point subflows stay about executing their ladder. This also composes with resume: a skipped
point still has its cells on S3, so the terminal render reads the complete grid regardless of what
re-ran.

### The render is a `@task`, not inline

The existing config-provenance artifact runs inline in the flow body because it is trivial (reads
baked files, publishes markdown). The render is different — it does fallible network I/O (S3
download, PNG upload) and matplotlib — so it is wrapped as a `@task`. The sweep's valuable work (the
cells) is persisted to S3 *before* the render, so a `@task` gives: a **retry** that re-renders
idempotently off S3 without re-running a cell, **failure isolation** so a render failure surfaces as
"sweep complete, render failed" rather than failing the whole run, and one render step on the
parent run's timeline.

It is **one** task, not split per artifact or per aggregator: the ceiling table and plot both come
from `aggregate_ceilings`, the cliff table and plot from `aggregate_rungs`, so splitting would
re-aggregate or shuttle large row-lists across task boundaries for a step that is seconds of
deterministic work.

### Four grid-level artifacts: tables as markdown, plots as images

The render publishes, keyed for versioning across runs:

- the **ceiling table** and the **goodput-cliff table** as `create_markdown_artifact` — text
  embedded, inline and durable forever, so the measured numbers never rot;
- the **ceiling plot** and the **goodput-cliff plot** as `create_image_artifact` fed a **presigned
  S3 URL** — inline in the UI while the run is fresh.

The PNG in S3 is the durable copy of each plot; only the inline-in-UI link expires with the
presigned URL. A durable-inline plot would require public hosting, which a private bucket forecloses
and which is not worth it here — the durable data is the tables (inline forever) and the PNGs in S3.
A base64 data-URI embedded in a markdown artifact would be durable-inline with no URL, but whether
Prefect's UI renders data-URI images is unverified; it is to be tried during implementation, falling
back to the presigned image artifact if the UI will not render it.

### The cost baseline stays offline

The `report` command's baseline $/1M-at-SLO table joins three separate tool outputs (`cost`,
`commercial-cost`, `prefix-cache`) — arms the knob sweep does not run. Surfacing it on the knob-sweep
run page would require the flow to drive those arms, which it does not. It stays an offline command;
only the four run-derived artifacts above go to the UI.

### Rejected alternatives

- **Per-point live artifacts.** Publishing a table per engine point as it completes, so the UI fills
  in mid-run. Rejected: the ceiling views are cross-point and degenerate at a single point, a
  per-point cliff duplicates what the grid-level faceted figure already shows, and it contradicts the
  terminal-render choice. (This is also the only thing that would have justified a separate pure
  `analysis` member, which ADR-0017 collapses.)
- **A separate render image or Kubernetes job** with only the plotting deps, to keep the worker image
  free of matplotlib. Rejected as YAGNI for a single-operator harness: it adds a second image build
  and job wiring for a step the worker can run directly. The worker's matplotlib is used by the
  render, not dead, so the lean-image argument that drove ADR-0017 does not apply to it.
- **An external dashboard (Grafana/MLflow/Streamlit).** The scale/aggregation/multi-audience answer;
  this harness has one operator and wants the plots on the run page. Reconsider if runs multiply or
  non-Prefect stakeholders need the visuals.

## Consequences

- The `orchestration` member gains a dependency on the `report` member (ADR-0017), and the worker
  image gains the plotting stack — used by this render task, the one concrete reason the worker
  carries it.
- The render task reuses `completion.py`'s S3-download pattern to materialize the run's cells into
  the layout the aggregators read; the new code is the wiring plus the PNG upload and the artifact
  publish, since the aggregators and renderers already exist.
- `create_image_artifact` needs a presigned URL from the private results bucket; the render mints one
  per plot. Old runs show a broken inline image once the URL expires — accepted, with the S3 PNG and
  the durable markdown tables as the lasting record.
- This ADR is prose only; the render task ships as a later ticket, after the ADR-0017 carve lands the
  `report` member it calls.
