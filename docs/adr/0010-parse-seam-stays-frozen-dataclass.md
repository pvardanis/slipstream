<!-- ADR recording why the result-JSON parse seam (LoadCell and its sibling record validators) stays a frozen dataclass with hand-written validation rather than a pydantic model, and where the line between pydantic and dataclass falls in this package (spec §3.1, epic #19 / issue #33). -->

# ADR-0010: The result-JSON parse seam stays a frozen dataclass, not pydantic

- Status: Accepted
- Date: 2026-09-22

## Context

`pydantic>=2` is already a dependency, introduced by ADR-0009 to validate the
human-authored sweep grid (`bench/sweep-grid.yaml`) against the `SweepGrid` model before
any GPU deploy. That raised a fair question while building the `chart` / `aggregate-sweep`
path: the aggregator reads a directory of `vllm bench serve` result JSON and validates each
record field by hand (`LoadCell.from_record` → `_require_int`, `to_numeric_metric`,
`classify_failures`), raising `SweepAggregationError` / `ResultError`. If pydantic already
guards one boundary, should it guard this one too?

The result JSON is reached from user-supplied paths via the CLI — `aggregate-sweep
--run-dir <dir>` reads every result file under a directory the user names, and the sibling
`cost` / `report` subcommands read a result file by path — so the seam **is** an
untrusted-input boundary, not merely trusted own-output. The knowledge base
(`python-idioms`, `python-project-setup`) draws the line plainly: plain dataclasses are the
default; pydantic is reserved for **validation at system boundaries — parsing untrusted
external input** (HTTP bodies, config files, CLI args). So both the "it's a boundary" and
the "use dataclasses by default" rules are in play at once, and the decision is which one
governs here.

## Decision

**The result-JSON parse seam stays a frozen dataclass with hand-written validation.**
pydantic is not adopted for `LoadCell` or its sibling record validators.

### The boundary is real, and it is already guarded

Being a boundary settles that the input *must* be validated, not that pydantic must do it.
The hand-validation already fails closed on a malformed file, every path tested, all
exiting 2 with a domain error that names the source path:

- not a file → `ResultError "result file not found: {path}"`
- not JSON → `ResultError "cannot read result {path}: {error}"`
- JSON but not an object → `ResultError "result {path} is not a JSON object"`
- object, missing/wrong-type field → `SweepAggregationError "result {source} missing or
  non-integer {key}"` (and the numeric / errors-list equivalents)

Every field is read through a guard (`_require_int`, `to_numeric_metric`,
`classify_failures`, `EnginePoint.from_dirname`'s regex); there is no raw `record[...]`
access that trusts a type. The boundary is guarded — what pydantic would add over it is
close to nothing.

### pydantic's strictness is a liability for this input, not a gain

Result JSON is **drift-prone**: `vllm bench serve` adds and renames fields across versions.
The hand-validation reads only the keys it needs and ignores the rest, so a benign new
upstream metric passes through untouched. pydantic earns its keep at a boundary through
`extra="forbid"`; here that would fail **closed** on a harmless added field — turning a
version bump into a broken aggregation. For drift-prone machine output, ignore-unknown is
the correct posture, and that is what the dataclass already does.

### Adopting it here would break a convention the package already keeps

pydantic sits at exactly one boundary — the human-authored config file — and frozen
dataclasses validate everything else, in two groups:

- **Human-authored config file** (`sweep-grid.yaml`) → **pydantic** (`SweepGrid`,
  `extra="forbid"`). A small, closed, hand-edited schema where forbidding unknown keys and
  rich coercion pay off, and a typo should fail loudly before a GPU spins up.
- **Machine-emitted result JSON** → **frozen dataclass with `from_record`** (`LoadCell` in
  `sweep_aggregation.py`, `Segment` in `report.py`). Drift-prone, read-only, validated
  field-by-field into an immutable value object — the seam this ADR is about.
- **CLI-supplied arguments** → **frozen dataclass with `__post_init__`** (`SweepConfig`,
  `CostInputs`, `CommercialCostInputs`), each raising its own domain error at construction.

That the CLI-argument value objects stay hand-validated — though CLI args are a boundary the
KB would allow pydantic for — is the tell: pydantic is the narrow exception reserved for the
config *file*, and hand-validation is the package default everywhere else.

`LoadCell` is one of two result-record seams and one of five hand-validated dataclasses.
Converting only it makes the seam layer *less* uniform — a lone pydantic validator beside
`Segment`'s hand-rolled twin. Converting the pair, or all five, is a large PR reaching well
outside issue #33, touching `ReportError` / `CostError` / `CommercialCostError` for no
safety gain.

### The exception contract would be unchanged regardless

Nineteen tests assert on the exact message text and field names of `SweepAggregationError`
/ `ResultError` (e.g. `"no knob-sweep points"`, `"missing or non-integer"`, the metric
names). A raw pydantic `ValidationError` reads nothing like those, so any adoption would
have to **catch `ValidationError` and re-wrap it** into the existing domain exceptions with
the same substrings to keep the contract. After that wrapping, pydantic has bought a
declarative field list and left the entire exception surface — the part callers and tests
actually depend on — exactly as it was.

## Consequences

- No code change and no new dependency: this is a documentation-only decision. The seam
  keeps `LoadCell.from_record` and the `results.py` readers as they are.
- The pydantic / dataclass boundary in this package is now recorded, not folklore: pydantic
  for human-authored config, frozen dataclass for machine-emitted records. New parse code
  picks its tool by which side of that line the input sits on.
- If result JSON ever grows a large, nested, or fast-moving schema — the case where a
  declarative model genuinely reduces hand-written boilerplate — this decision is revisited
  for both result-record seams (`LoadCell`, `Segment`) together, so the convention stays
  uniform rather than splitting.
