<!-- ADR recording why a knob-sweep is turned into a server-config recommendation by a prose-only Claude Code skill (analyze-bench) rather than new package code; why the skill reads the completed Prefect flow run over the REST API as its only source — filtering flow runs by the run= tag to the parent, then reading that parent's config/ceiling/cliff markdown artifacts and the two plot URLs — instead of syncing S3 or calling the report CLI; why the recommendation ranks engine configs by output_throughput at the ceiling, which is the exact cost ordering because the sweep runs on one fixed GPU so $/hr is constant and $/1M-tokens is inversely proportional to throughput; why a $/hr price is optional and only prints an absolute figure without changing the ranking; why prefix_share is treated as a workload property the recommendation reasons over rather than a server knob it selects; and why a bespoke winner-highlighted chart is deferred to a tested report-package plot function rather than a throwaway snippet in the skill. Builds on ADR-0018 (knob sweep renders results to the Prefect UI) and ADR-0019 (SLO gates and token rate per rung). AMENDED 2026-10-19: the two plots are downloaded into the report directory and embedded by relative path, since the results bucket and its URLs die on teardown. AMENDED 2026-10-22: a sibling skill, analyze-kv-ab, reports the KV cache A/B's verdict on the same terms, judging each rung by whether the arms' ranges separate, and reads each run's own artifact versions rather than the latest per key. AMENDED 2026-10-22 (later): report directories are named by run kind, reports/sweep-grid-<stamp>/ and reports/kv-cache-ab-<run_id>/, and the KV A/B report names LMCache as the offload and states why the GPU KV pool is capped. AMENDED 2026-10-22 (rename): the skill is renamed analyze-knob-sweep, after the run kind it reads, beside analyze-kv-ab. -->

# ADR-0021: analyze-bench reads the run and ranks by throughput

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

## Amendment (2026-10-19): the report keeps its own copy of the plots

Linking the plots by their S3 URL leaves them dead after the first teardown: the results
bucket is created with `force_destroy` and deleted on `bench-endpoint-down`
(`terraform/bench-endpoint/results.tf`), and the next `stack-up` names a fresh bucket from
`bucket_prefix`, so no URL outlives the cycle that made it. The report is meant to outlast
both the bucket and the Prefect run, so the skill now downloads the two PNGs from their
public URLs into `reports/<run_id>/` beside `RECOMMENDATION.md` and embeds them by relative
path. The report directory is no longer all text; two committed PNGs are the price of
plots that still render. The download is a plain `curl` of the two
public chart objects, not an S3 sync, so the skill still needs no AWS credentials, repo
code, or report CLI. A plot is kept only when its download completes and is a PNG; a
plot that cannot be fetched degrades the report the way a missing artifact does, since
the ranking never depends on it.

## Amendment (2026-10-22): a sibling skill reports the KV cache A/B

The KV cache A/B (#326) asks a different question of a different run: not which engine
config to deploy, but whether turning KV cache offload on beat leaving it off. Its run
carries its own artifacts (`kv-ab-config`, `kv-ab-rung-spreads`, `kv-ab-ceilings`,
`kv-ab-plot`), so it gets its own prose-only skill, `analyze-kv-ab`
(`.claude/skills/analyze-kv-ab/SKILL.md`), built on the same terms as this one: the
completed Prefect run is the only source, the SLO and workload are read from the run's
config artifact, and the chart is copied into `reports/<run_id>/` beside the report.

The verdict rests on separation: a metric favours an arm at a rung only when the two
arms' ranges across their runs do not overlap. With a handful of runs per arm that is the
strongest claim the data supports; complete separation with `n` runs per arm has a
one-sided permutation p of `1 / C(2n, n)`, which the report states. Cost follows this
ADR's argument unchanged: both arms run on the same node, so the throughput ratio at the
ceilings is the cost ratio.

The skill reads each run's own artifact versions (`/artifacts/filter` by flow run,
newest first), because `/artifacts/latest/filter` returns a key's newest version across
all runs and so comes back empty for any run that is not the newest writer of that key.

## Amendment (2026-10-22, later): report directories are named by run kind

`reports/<run_id>/` does not say which flow produced a report, and a run id is free-form, so a
listing of `reports/` mixes knob sweeps and A/Bs with nothing to tell them apart. Each skill now
prefixes its directory with the run kind. `analyze-bench` writes `reports/sweep-grid-<stamp>/`,
where `<stamp>` is the `run_id` with any leading `sweep-full-` dropped, so
`sweep-full-20261008T130350Z` lands in `reports/sweep-grid-20261008T130350Z/`. `analyze-kv-ab`
writes `reports/kv-cache-ab-<run_id>/`. The report still names its run by the `run_id` itself,
since that is the Prefect `run=` tag. `.gitignore` admits each skill's chart by file name,
`kv-ab-by-max-concurrency.png` beside the knob sweep's two.

The KV A/B report also says what "offload" is and why the GPU pool is small. The on arm is vLLM
with KV cache offload to LMCache's CPU tier (`LMCacheConnectorV1` in `kv_both` role); the off
arm has no KV connector, and both keep the GPU prefix cache. When the workload sets
`engine.kv_cache_memory_bytes`, the report states that the GPU KV pool was capped below the
tier because the g5.xlarge's host RAM cannot hold a tier larger than the uncapped pool, and
that the g5.2xlarge which could is waiting on a vCPU quota increase (ADR-0023). Every cost
ratio it quotes is labelled as holding on the capped pool, not on the uncapped rig.

## Amendment (2026-10-22, rename): the skill is named after the run kind it reads

`analyze-bench` reads a knob sweep, but its name does not say so, and its sibling
`analyze-kv-ab` is named after its run kind. The skill is renamed `analyze-knob-sweep`
(`.claude/skills/analyze-knob-sweep/SKILL.md`), so each skill's name says which run it
analyses. Its behaviour and report directory are unchanged. The sections above keep the name
the skill had when they were decided.
