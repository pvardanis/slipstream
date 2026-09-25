<!-- ADR recording why the single SweepConfig splits into a shared Knobs base with two subclasses — CellConfig (knobs + one grid coordinate) and SweepConfig (knobs + grid axes) — so load-cell takes a real cell config instead of coordinate flags plus a whole-grid YAML it ignored, and why the per-cell fan-out enumerates SweepConfig.cells() in Python to issue one remote docker run per cell rather than shelling load-cell in a loop. Rests on ADR-0011 (config crosses the pydantic boundary) and sharpens ADR-0012 §Amendment (the container runs one cell). Issue #155, PR #162. -->

# ADR-0014: One Knobs base, a CellConfig and a SweepConfig, and no autoregressive CLI

- Status: Accepted
- Date: 2026-09-25

## Context

[ADR-0011](0011-config-inputs-cross-the-pydantic-boundary.md) moved the
experiment-defining knobs into a per-command YAML validated at the pydantic
boundary; `load-sweep` parses a `SweepConfig`.
[ADR-0012](0012-sweep-resumability-and-orchestrator-choice.md) §Amendment then
relocated the Tier-2 grid loop out of the bench-client and into the orchestration
layer, changing the container's entrypoint from "loop the whole grid" to **execute
the single cell it is handed** — one `vllm bench serve` per `docker run` — with
`cell_command` (the pure argv builder) staying shared.

That amendment named the cell as the execution unit but left the **type** of a cell
implicit. The container's single-cell command, `load-cell`, took its grid coordinate
as CLI flags (`--share`, `--burstiness`, `--max-concurrency`) **and** a
`--config load-sweep.yaml` — a `SweepConfig`, whose three grid axes
(`prefix_shares`, `burstiness_values`, `max_concurrency_values`) it read the shared
knobs from but otherwise ignored. Those axes have no schema default, so a config fed
to `load-cell` had to declare a grid it never ran: dead-but-mandatory data. One
`SweepConfig` was doing two jobs — describing a grid and, minus its axes plus three
loose flags, describing a single cell — and the single-cell job carried the grid job's
baggage.

## Decision

### One `Knobs` base; `CellConfig` and `SweepConfig` are its two subclasses

The shared knobs and their cross-field validators (`num_prompts >= num_prefixes`, the
commercial-run tokenizer requirement) move to a `Knobs(BaseModel)` base with the
boundary's `ConfigDict(extra="forbid", frozen=True)`. Two subclasses add only what
distinguishes them:

- **`CellConfig(Knobs)`** adds the one coordinate a cell runs: `prefix_share`,
  `burstiness`, and an optional `max_concurrency` (set = closed-loop cap, omitted =
  open-loop). The coordinate is range-checked by the field types, not by hand.
- **`SweepConfig(Knobs)`** adds the grid axes (`prefix_shares`, `burstiness_values`,
  `max_concurrency_values`) and a `cells()` generator that yields one `CellConfig` per
  grid point, shares outermost and the concurrency ladder innermost.

Inheritance, not composition or a flag, because the two configs **are** the same knobs
seen at two grains: a point on the grid and the grid itself. The base owns the shared
validation once, so the single-cell and whole-grid paths cannot drift in how a knob is
checked — the Duplicated-Code tell of two hand-kept validators is removed by having one.

### `load-cell` takes a real `CellConfig`; no coordinate flags

`load-cell` parses a `CellConfig` YAML (`bench/load-cell.yaml`) carrying the coordinate
plus the shared knobs, and drops `--share`/`--burstiness`/`--max-concurrency`. A cell is
now a reviewable artifact in the same category ADR-0011 put the sweep in — a
human-authored, `extra="forbid"` config — reached through the same loader boundary
(`load_cell_config` alongside `load_sweep_config`, sharing the file-read, reserved-key
guard, and validation body). The execution context (`--base-url`, `--model`,
`--out-dir`, commercial flag) stays CLI-injected in both, since `model.yaml` is the
served-model source of truth and must not be duplicated into the config file.

The rejected alternative was an axis-free knobs YAML plus the three coordinate flags.
It keeps the coordinate on the command line — the exact split ADR-0011 closed for the
sweep — and re-opens the two-validation-layer duplication (Typer constraints plus model
checks) that ADR-0011 collapsed. Putting the coordinate on the type is the same move,
applied to the cell.

### The per-cell fan-out is `SweepConfig.cells()` in Python, not a CLI loop

The orchestration layer (ADR-0012) enumerates the grid by iterating
`SweepConfig.cells()` **in Python**, rendering each `CellConfig` and issuing **one
remote `docker run` per cell** (the rendered config mounted as `--config`). It must
**not** shell `slipstream-bench load-cell` in a loop. The per-cell execution unit is the
remote `docker run` on the bench endpoint; the CLI is the container's entrypoint for the
one cell it is handed, never a step the orchestrator drives autoregressively. Only the
sweep path wears Prefect (ADR-0012); a single cell is a direct `docker run`.

This keeps one enumerator — `cells()` — as the single source of the grid's shape. A CLI
loop would put the product of the axes in bash and leave `cells()` and the loop as two
places the grid order could diverge.

### The local `run_sweep` / `load-sweep` stays

`load-sweep` and its in-process `run_sweep` (which now iterates `config.cells()`) are
kept for local, Prefect-free whole-grid runs. Removing them in favour of the
orchestrated path only is not taken now; revisit when the orchestrator is the sole
supported driver.

## Consequences

- `load-cell`'s config is a `CellConfig`; `bench/load-cell.yaml` (renamed from
  `bench/cell.yaml` to mirror the subcommand, as `load-sweep.yaml` mirrors `load-sweep`)
  is the committed default. Both YAMLs are excluded from the bench-image content hash
  (`bench/image-tag.sh` and the CI `paths-filter`): they are runtime inputs mounted into
  the container, not baked into the image.
- An out-of-range coordinate now fails with the pydantic field message (e.g. "less than
  or equal to 100") wrapped in "invalid cell config (path)", rather than the former
  hand-written "--share 150 out of range" flag error. This is inherent to validating the
  coordinate on the type, and is accepted: the cell config is orchestrator-rendered, not
  hand-typed at a flag, so the field message is the right grain.
- The result-JSON key and the swept axis agree on a name: the cell field is
  `prefix_share`, matching `SweepConfig.prefix_shares` and the `prefix_share` key the
  baseline report segments on.
- This ADR is prose only; the type split and the CLI change ship in PR #162 (issue #155).
  The orchestrated fan-out that consumes `cells()` is the orchestration layer's own work
  (ADR-0012), not this PR.
