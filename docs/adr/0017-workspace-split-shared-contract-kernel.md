<!-- ADR recording why the single slipstream-bench distribution is split into a four-member uv workspace (contract, bench, report, orchestration) under a shared PEP-420 slipstream namespace, with a pure-data contract kernel every layer depends inward on and neither executor nor worker importing the other: the split strips the dead pandas/seaborn plotting stack from both deployment images, names the config-in/result-out contract the layers already pass across the SSM seam, and inverts the orchestration->bench edge into orchestration->contract<-bench. Tripped the split deferred by ADR-0012 §Amendment. See ADR-0018 for the render-to-UI work that puts the orchestration->report edge in. -->

# ADR-0017: The orchestration/bench/report split into a uv workspace on a shared contract kernel

- Status: Accepted
- Date: 2026-10-05

## Context

Today the repo is one distribution, `slipstream-bench` (one `pyproject.toml`). The
orchestration layer is the in-repo subpackage `slipstream_bench.orchestration`, gated behind
the `[orchestration]` extra (ADR-0012 §Amendment). Two images build from the one source tree:
`bench/Dockerfile` installs `.` (the per-cell executor, meant to be Prefect-free),
`orchestration/Dockerfile` installs `.[orchestration]` (the Prefect worker).

There is **no dependency cycle**. The edge is strictly one-way — `orchestration` imports
`slipstream_bench.sweep.*` and `slipstream_bench.results`, core bench imports orchestration
never — and that direction is already enforced by `tests/orchestration/test_sweep_is_prefect_free.py`
and documented at `src/slipstream_bench/orchestration/__init__.py`. What reads as a tangle is two
separate things: an SSM runtime round-trip (worker sends a cell command, the host runs it, the
result lands in S3, the worker reads it back) and a **shared distribution** that leaves the
config-in/result-out contract unnamed and interleaved with execution code.

Two concrete smells follow from the single distribution:

- **Dead dependencies in both images.** `pandas` and `seaborn` are base dependencies
  (`pyproject.toml`). The per-cell executor never imports them, and neither does the worker, yet
  both images carry the whole plotting stack because one distribution installs wholesale. The
  subpackage-plus-extra boundary cannot strip them (ADR-0012 §Amendment deferred the hard split;
  this ADR takes it).
- **An unnamed shared contract.** `orchestration` reaches into six core-bench modules for ~13
  symbols — `sweep.config` (`CellConfig`, `RESERVED_KEYS`), `sweep.grid` (`SweepGrid`,
  `build_point_sweep_config`, `load_grid`, `list_engine_points`), `sweep.aggregation`
  (`EnginePoint`, `LoadCell`, `SweepAggregationError`), `sweep.runner` (`get_cell_basename`),
  `results` (`ResultError`, `read_result`). These are the config the worker base64-ships into a
  cell and the result shape it reads back — a data contract, not reaching into internals — but it
  lives scattered across modules that also hold execution code, so the imports read as a leak.

The knowledge base (`domain-driven-design.md`) blesses a **shared kernel** that two contexts both
depend on inward, neither importing the other — "DIP at the context level" — on three conditions:
the kernel stays small and stable, holds pure data not behaviour, and both sides genuinely agree on
one model. This split satisfies all three: the config-in/result-out types are small and pure, and
the worker and executor share exactly one model of them (the same base64 config, the same result
JSON). The KB has no guidance on uv workspaces or PEP-420 namespaces; those mechanics are recorded
here as net-new.

uv workspace facts (confirmed against current uv docs): members each own a `pyproject.toml`, the
workspace shares **one lockfile** (so no independent-resolution cost or benefit), intra-workspace
deps wire through `[tool.uv.sources] <member> = { workspace = true }`, and a PEP-420 namespace is
enabled by `[tool.uv.build-backend] namespace = true` with a `src/slipstream/<member>/` layout and
no `__init__.py` at the `slipstream/` root.

## Decision

### Four workspace members on a shared pure-data kernel

The one distribution becomes four members, every one depending **inward** on a shared kernel and no
other:

- `contract` — the pure-data kernel. No framework, no heavy deps (pydantic, PyYAML, stdlib).
- `bench` — the per-cell executor: `sweep/runner.py` (`cell_command`/`execute_cell`/`run_sweep`),
  `cost/`. Runs in `bench/Dockerfile`. Depends on `contract`.
- `report` — the offline analysis and visualization: the multi-cell aggregators and the table and
  plot renderers. Carries the plotting stack. Depends on `contract`.
- `orchestration` — the Prefect worker: flows, tasks, cluster, SSM, digest, config artifact.
  Depends on `contract` (and, for the render task of ADR-0018, on `report`).

Edges: `bench → contract`, `report → contract`, `orchestration → contract`. The
`orchestration → bench` edge that read as a leak is **gone**, not narrowed — the worker now depends
on the named kernel, never on the executor. There is no cycle.

### The contract kernel holds pure data, not behaviour

What moves into `contract`: the config and grid types and their loaders (`CellConfig`,
`RESERVED_KEYS`, `SweepGrid`, `build_point_sweep_config`, `load_grid`, `list_engine_points`, the
`fields` leaf), the shared value objects and their single-record parse (`EnginePoint`, `LoadCell`
with `from_record`, `SweepAggregationError`), the result parse (`ResultError`, `read_result`,
`to_numeric_metric`), and the pure naming helpers (`get_cell_basename`, `split_lengths`).

`LoadCell.from_record` and its per-record helpers (`goodput_fraction`, `classify_failures`) go in
the kernel despite carrying logic: they are the **deserialization of one result record** into a
frozen value object — the same category as `read_result`, which the kernel already owns — not
behaviour both sides mutate. The KB's line is that a method-bearing *aggregate* two contexts reach
into needs its own interface; a frozen DTO with a parse constructor is contract. The **multi-cell**
aggregation (folding a run into ceiling/cliff rows) is analysis, not contract, and stays in
`report`.

### `report` isolates the plotting stack; the executor image loses it

`pandas`, `seaborn`, and `matplotlib` become dependencies of `report` alone. The per-cell executor
(`bench`) and its image no longer carry them. The worker gains them only through its dependency on
`report` for the render task (ADR-0018), where they are **used**, not dead. This is the clean-image
outcome the split is taken for: the executor image strips the plotting stack entirely, and the
plotting stack lives in exactly one member.

### uv workspace under a shared `slipstream` namespace

The members live under `packages/{contract,bench,report,orchestration}/`, each with its own
`pyproject.toml` and `src/slipstream/<member>/` tree, under a workspace root that declares
`[tool.uv.workspace] members = ["packages/*"]`. PEP-420 namespace packaging gives domain-reading
imports — `from slipstream.contract import CellConfig`, `slipstream.orchestration.flows`,
`slipstream.bench.runner`. One lockfile, one version across members. Images install their member
(`uv sync --package bench` / `--package orchestration`), each pulling `contract` transitively.

### `CeilingScrapeError` moves to orchestration

`CeilingScrapeError` lives in `sweep.aggregation` today but only orchestration raises or references
it; it is not part of the result-aggregation data flow. It moves into the orchestration layer where
its one use is, rather than into `contract`.

### The import guard extends to the new boundaries

`tests/orchestration/test_sweep_is_prefect_free.py` — the AST walk forbidding a `prefect` import in
the Prefect-free code — extends to the member boundaries: `contract` imports neither framework nor
any other member; `bench` and `report` import `contract` only (never each other, never
`orchestration`); `contract` and `bench` import no `prefect`. The inward-only rule becomes a
checked invariant across the workspace, not just a convention in one package.

### Rejected alternatives

- **Keep one distribution, split only into subpackages (the ADR-0012 §Amendment shape).** Cannot
  strip the dead plotting stack from the images — one distribution installs wholesale. It gives the
  one-way boundary (already had it, AST-enforced) and nothing the images need.
- **Separate repositories or packages published to an index.** Reintroduces the independent-release
  ceremony and version-sync the ADR-0012 deferral was avoiding, for benefits (independent cadence,
  external consumption) that still have no buyer. A uv workspace delivers the clean split at one
  version and one lockfile; the published-package step waits for its own trigger.
- **A separate `analysis` member (pure aggregation + table rendering) distinct from `report` (heavy
  plotting).** Collapsed into one `report`: aggregation, table rendering, and plotting have the same
  two consumers (the render task and the offline CLI), so the split bought nothing. It would have
  earned its place only under per-point live tables published by the worker from pure code, which
  ADR-0018 rejects.

## Consequences

- Every cross-member import is renamed (`slipstream_bench.sweep.config` → `slipstream.contract.config`,
  and so on across all call sites). Mechanical, wide, one pass.
- Both Dockerfiles change their install target to the relevant workspace member; the two entry-point
  scripts (`slipstream-bench`, `slipstream-orchestrate`) move to their members; the AST guard test
  is extended.
- The executor image drops `pandas`/`seaborn`/`matplotlib`. The worker image gains them (used, via
  `report`) — a larger image, a smaller and different concern than dead dependencies.
- `report/chart.py` is split at the module level: its pure table renderers (`rows_to_markdown`,
  `rows_to_json`, `rungs_to_markdown`, `rungs_to_json`) separate from its matplotlib plotters, so a
  caller can render tables without importing the plotting stack. Both stay in `report`.
- This ADR is prose only; the carve ships as a later stack, one kind of change each: the guard test,
  then `contract`, then `report`, then `bench`/`orchestration` with the workspace and namespace.
