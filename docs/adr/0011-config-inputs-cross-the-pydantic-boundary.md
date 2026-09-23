<!-- ADR recording that the experiment-defining config inputs (SweepConfig, CostInputs, CommercialCostInputs) become human-authored config-file inputs and so cross into the boundary pydantic guards, superseding ADR-0010's classification of them as CLI args validated by a frozen-dataclass __post_init__. The result-JSON parse seam (LoadCell) is unchanged (spec §3.1, epic #126 / issue #127). -->

# ADR-0011: The experiment-defining config inputs cross into the pydantic-guarded boundary

- Status: Accepted
- Date: 2026-09-23

## Context

[ADR-0010](0010-parse-seam-stays-frozen-dataclass.md) recorded the pydantic /
frozen-dataclass line in `slipstream-bench` and
sorted every parse seam into three groups: the human-authored config file
(`sweep-grid.yaml` → `SweepGrid`, **pydantic**), machine-emitted result JSON
(`LoadCell`, `Segment` → **frozen dataclass** with `from_record`), and
CLI-supplied arguments (`SweepConfig`, `CostInputs`, `CommercialCostInputs` →
**frozen dataclass** with `__post_init__`). At the time, the knobs that define a
run reached the package as CLI options: `load-sweep` took 18, `cost` and
`commercial-cost` six each, and each value object re-validated those options in
its `__post_init__` — a validation layer sitting behind Typer's option
constraints, which the dataclass comment itself flagged as a duplication.

Epic #126 changes what those three value objects parse. A benchmark run becomes a
versioned artifact: the knobs that define an experiment move out of shell history
and into a per-command YAML file that is committed and reviewed in the pull
request proposing the run. The temporal line is the seam — everything knowable
before a run exists and reviewable in the diff (grid axes, SLOs, cost provenance,
commercial rates and quote date) moves into YAML; execution context and run
artifacts (endpoint, credentials env var, output directory, dry-run flag,
result-file paths) stay as CLI arguments, because they are environmental or do not
exist until after the run.

That move reclassifies `SweepConfig`, `CostInputs`, and `CommercialCostInputs`.
They are no longer parsed from CLI arguments; they are parsed from a human-authored
config file — the exact input category ADR-0010 (and the knowledge base it cites:
`python-idioms`, `python-project-setup`) reserves pydantic for. The classification
that governed them changes, so it needs a written trail rather than being folded
silently into the refactor.

## Decision

**`SweepConfig`, `CostInputs`, and `CommercialCostInputs` become pydantic models,
joining the human-authored config-file boundary group.** Each is now parsed from a
per-command YAML file (following the `sweep-grid.yaml` precedent), validated by a
pydantic model with `extra="forbid"`. Their `__post_init__` validation — the range
checks, the duplicate-axis rejection, the pinned-provenance requirement, the
commercial-quote invariants — moves into the pydantic model, and the Typer option
constraints on these commands are removed. The meaty validation lives once, in the
model, at the boundary the input actually crosses.

This is the correct side of ADR-0010's own line. These inputs are now:

- **A human-authored config file.** Small, closed, hand-edited schemas where
  forbidding unknown keys catches a typo before a GPU spins up, and rich coercion
  earns its keep. This is precisely the case ADR-0010 gave pydantic.
- **Reviewed in the diff.** A run's identity — its grid, its cost provenance, its
  commercial quote and date — is now a reviewable artifact, so failing loudly and
  early on a malformed field is the right posture, exactly as it is for
  `sweep-grid.yaml`.

The duplication ADR-0010 noted as a tell (CLI args re-validated in
`__post_init__`) is resolved by the same move: with the definition living in YAML,
there is one validation layer, not a Typer layer plus a dataclass layer.

### What this supersedes, and what it leaves intact

This decision **supersedes the ADR-0010 classification** that grouped
`SweepConfig`, `CostInputs`, and `CommercialCostInputs` under *CLI-supplied
arguments → frozen dataclass with `__post_init__`*. ADR-0010 read their staying
hand-validated as the tell that pydantic was the narrow exception for the config
*file*; once these inputs **are** a config file, that reading no longer applies to
them, and they move into the pydantic group where the same rule now places them.

Everything else in ADR-0010 stands:

- **The result-JSON parse seam is unchanged.** `LoadCell` and its sibling
  `Segment` stay frozen dataclasses with hand-written `from_record` validation.
  Result JSON is machine-emitted and drift-prone; ADR-0010's reasons for keeping
  it ignore-unknown (a benign new upstream metric must pass through, not fail
  `extra="forbid"`) are untouched by this decision. This work does not cross that
  seam.
- **The line itself is unchanged** — pydantic for human-authored config, frozen
  dataclass for machine-emitted records. What changes is only which side three
  value objects sit on, because their input category changed.

## Consequences

- The pydantic-guarded boundary now covers four config files, not one:
  `sweep-grid.yaml` plus the per-command experiment definitions for `load-sweep`,
  `cost`, and `commercial-cost` (and `prefix-cache`, per the epic). A run is a
  directory of small, reviewable YAMLs.
- Callers of these commands reason about a single validation layer. The Typer
  option constraints and the `__post_init__` re-checks collapse into one pydantic
  model per command.
- This ADR is the written trail the epic requires before the seam pull requests
  land; it is documentation-only and blocks nothing. The model conversions
  themselves arrive in the per-concept pull requests (issues #129, #130, #131),
  each test-first against the CLI seam and the model's `model_validate`.
- The pydantic / dataclass split stays governed by input category, not by module:
  new config-file parsing reaches for pydantic, new machine-record parsing reaches
  for a frozen dataclass, and this ADR is the precedent for reclassifying a value
  object when the input it parses moves across that line.
