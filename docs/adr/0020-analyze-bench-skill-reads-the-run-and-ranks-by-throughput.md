<!-- ADR recording why a knob-sweep is turned into a server-config recommendation by a prose-only Claude Code skill (analyze-bench) rather than new package code; why the skill reads the completed Prefect flow run over the REST API as its only source — filtering flow runs by the run= tag to the parent, then reading that parent's config/ceiling/cliff markdown artifacts and the two plot URLs — instead of syncing S3 or calling the report CLI; why the recommendation ranks engine configs by output_throughput at the ceiling, which is the exact cost ordering because the sweep runs on one fixed GPU so $/hr is constant and $/1M-tokens is inversely proportional to throughput; why a $/hr price is optional and only prints an absolute figure without changing the ranking; why prefix_share is treated as a workload property the recommendation reasons over rather than a server knob it selects; and why a bespoke winner-highlighted chart is deferred to a tested report-package plot function rather than a throwaway snippet in the skill. Builds on ADR-0018 (knob sweep renders results to the Prefect UI) and ADR-0019 (SLO gates and token rate per rung). -->

# ADR-0020: analyze-bench reads the run and ranks by throughput

- Status: Accepted
- Date: 2026-10-09

## Context

A knob sweep ends with its results on the parent flow run's Prefect page
(ADR-0018): a config artifact embedding the run's `sweep-grid.yaml`, a ceiling
table carrying `output_throughput` and the two p95 SLO gates per rung (ADR-0019),
a goodput cliff, and two plots. What the sweep does not produce is the decision an
operator actually wants: *which engine config do I deploy for this SLO, and why.*
That last step is judgement over numbers that already exist — reading the ceiling
table, weighing contenders, naming the tradeoff — not more computation.

The `report` package already aggregates ceilings and rungs and computes a
baseline `$/1M-tokens` join. Re-deriving any of that in a second place would risk
disagreeing with what the sweep published. The goodput SLO (`ttft`/`tpot`
thresholds) lives faithfully, keyed to the run, in exactly one place: the
`knob-sweep-config` markdown artifact on the parent run. It is not in the S3 cell
JSON (which carries only `request_goodput`/`request_throughput` rates), and the
`0.95` floor is a source constant (`packages/report/src/slipstream_bench/report/aggregation.py:28`),
not per-run config. The current working-copy `bench/sweep-grid.yaml` may have
drifted from what the run used, so it is not a faithful SLO source.

The whole sweep serves one model on one fixed GPU (`k8s/vllm-gpu.yaml`:
`--tensor-parallel-size 1`, an 8B model on a single A10G). Every engine config
therefore runs at the same `$/hr`.

## Decision

Ship the recommendation as a prose-only Claude Code skill, `analyze-bench`
(`.claude/skills/analyze-bench/SKILL.md`), not as package code or a CLI command.
The skill's only input is a `run_id`; its only data source is the completed
Prefect flow run, read over the REST API:

1. `POST {PREFECT_API_URL}/flow_runs/filter` by tag `run=<run_id>`; the parent is
   the run whose name is not an engine-point slug (`mns\d+_kv(fp8|fp16)_pc(on|off)`).
2. `POST {PREFECT_API_URL}/artifacts/filter` for that parent; read the SLO from the
   `knob-sweep-config` artifact, the ranking data from the `knob-sweep-ceiling-table`,
   the fall diagnostics from `knob-sweep-goodput-cliff`, and the two plot PNGs from
   the image artifacts' S3 URLs.

No S3 sync, no report CLI, no new package code. The SLO is read from the run, never
asked of the user.

Rank engine configs by `output_throughput` at the ceiling. Because `$/hr` is constant
across configs on the fixed GPU, `$/1M-tokens` is inversely proportional to throughput,
so the throughput ordering *is* the cost ordering — exactly, without needing a price. A
`$/hr` is an optional input used only to print an absolute `$/1M-tokens` figure; it does
not change the ranking.

A server config is `(max_num_seqs, kv_cache_dtype, prefix_caching on/off)`.
`prefix_share` is a workload property, not a deployable knob, so the skill reasons about
which share bucket matches the target workload rather than picking the highest-throughput
row across all buckets.

The report (`reports/<run_id>/RECOMMENDATION.md`, outside `bench/` so it does not sit
among shipped code and images) always names the runner-up and why it lost. It is all
text: a trimmed evidence slice of the tables — the top N configs by throughput per
prefix-share bucket and the recommended config's cliff rungs, with the full tables left
on the Prefect run — plus the two plots linked by their public S3 URL (the
`sweeps/*/charts/*` prefix is public-read by bucket policy) rather than downloaded. With
no binaries and no 240-row dumps, the report is a decision document, committed to the
repo under `reports/<run_id>/`. A bespoke winner-highlighted chart is deferred: it belongs
as a tested plot function in the `report` package, not a throwaway plotting snippet in
the skill.

## Consequences

The recommendation stays in sync with the sweep by construction — it reads the published
artifacts rather than recomputing, so it cannot disagree with the run page. It needs the
Prefect server reachable (the `just prefect-ui` port-forward, or the EKS release); the
skill fails fast on `/health` if it is not. It reports only for the SLO the sweep was run
under — a different SLO means a new sweep, not a re-gate. The cost ranking is exact only
while the sweep stays on one fixed GPU; a multi-GPU or mixed-hardware sweep would break
the `$/hr`-constant assumption and require real per-config cost, at which point the
ranking moves into tested `report` code.
