<!-- ADR recording the rewrite of the L0 benchmark harness from bash to a Python package, its packaging, CLI, dependency stance, and test approach. -->

# ADR-0003: L0 benchmark harness in Python

- Status: Accepted
- Date: 2026-09-11

## Context

The L0 benchmark harness (#18) is three bash scripts: a `vllm bench serve`
wrapper (#27), a cost-per-1M post-processor (#28, arithmetic in `jq`), and a
prefix-cache-hit scraper (#29, Prometheus exposition parsed in `awk`, joins in
`jq`). Three facts shape the decision to move them to Python:

1. **The numbers are defended.** #18's definition of done is a measured
   `$/1M self-hosted vs commercial at SLO` that rolls up to Map #1. Cost-per-1M
   and the cold/warm hit-rate delta are arithmetic and JSON-join correctness that
   `jq`/`awk` make genuinely risky (the scraper already carries a `%.0f`-vs-`%g`
   big-counter guard). Wrong output from a benchmark is worse than ugly code.
2. **Benchmarking is a growth area.** Later layers add knob sweeps, LMCache
   wins, and more post-processors on the same result JSON. The harness will grow.
3. **It is portfolio-visible.** The repo demonstrates infra skill; the harness
   should read as such, not as a bash chunk grown past what bash is good at.

The `.pre-commit-config.yaml` already wires `ruff` + `ruff-format`, exercising
nothing today because the repo has no Python.

## Decision

**Rewrite all three as a `slipstream-bench` Python package** with a `typer` CLI
exposing `serve-sweep`, `cost`, and `prefix-cache` subcommands.

- **Tooling: `uv` + `pyproject.toml` + `uv.lock`.** Dev dependencies are
  `pytest` and `ruff` (the pre-commit gate now exercises real code).
- **Runtime dependencies stay slim** but are allowed — the tools run inside a
  baked image (below), so the environment is controlled. `prometheus-client`
  parses the exposition format in `prefix-cache` instead of hand-rolled text
  scanning.
- **A baked bench-client image** carries the package, the vLLM tokenizer, and
  the dependencies. Everything runs in that image; nothing is `kubectl cp`-ed in.
  Today the image is launched **in-cluster** — it replaces the stock vLLM image
  in the bench-client pod and reaches vLLM over ClusterIP, exactly as now. The
  vantage change is separate (ADR-0004) and does not block this work.
- **`serve-sweep` takes `--base-url` and assumes no cluster.** Where it runs and
  what it targets is orchestration, not tool logic.
- **Tests are fresh `pytest`, seeded from a checklist** mined off the existing
  bash tests so no edge case is silently dropped: big-counter formatting,
  `_total`-suffix matching, counter restart/backwards windows, integer
  prefix/suffix split truncation, run-to-run reproducibility, and the `jq`
  type/`> 0` guards. Each pull request deletes the bash script and its bash test
  in the same change that lands the Python replacement.

## Consequences

- Because the tools ship in a controlled image, there is no stdlib-only
  constraint and no split between a host CLI and an in-pod entrypoint — `typer`
  is the single front end.
- The work splits into sub-issues under #18: `2b` package + uv scaffold, `2c`
  bench-client image, then `3b`/`4b`/`5b` for the three tools. `4b` and `5b`
  share a result-JSON reader; `serve-sweep` writes via vLLM and reads nothing, so
  it shares nothing.
- The in-cluster ClusterIP launch is preserved. Moving the load generator to an
  external vantage is a separate, deferred decision (ADR-0004).
- Fresh tests risk dropping an edge case the bash encoded; mining the bash tests
  as a checklist is the mitigation, not byte-for-byte output parity.
