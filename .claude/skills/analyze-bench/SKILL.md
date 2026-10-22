---
name: analyze-bench
description: Recommend the best vLLM engine config from a knob-sweep benchmark. Use when the user gives a knob-sweep run_id or `run=` tag and wants the best server configuration for that run's SLO, or a report analysing a sweep's ceiling/goodput results.
---
<!-- Reads a completed knob-sweep Prefect flow run over the REST API and writes a server-config recommendation report for the run's own SLO. Prose-only: no repo code, no S3 sync, no report CLI. -->

# analyze-bench

Given a knob-sweep `run_id`, read its completed Prefect flow run and write a recommendation: which vLLM engine config to deploy for the SLO the sweep was run under, and why. The run already carries every number and plot (ADR-0018); this skill reads them and reasons — it does not re-run the sweep, sync S3, or call the report CLI.

Input: a `run_id` (e.g. `sweep-full-20261008T130350Z`). Optional: a GPU `$/hr` price — supply it only to print an absolute `$/1M-tokens` figure; the ranking never needs it.

## Preconditions

The Prefect REST API must be reachable. Resolve the base once:

```bash
API="${PREFECT_API_URL:-http://127.0.0.1:4200/api}"
curl -fsS -m 5 -o /dev/null -w '%{http_code}' "$API/health"
```

If curl exits non-zero or the code is not `200`, stop and tell the user to bring the port-forward up with `just prefect-ui`. Do not proceed against an unreachable server.

Every call below uses `-fsS` so a transport error, 4xx, or 5xx exits non-zero with a message rather than returning a body the agent would misread. On any such failure, stop and report the HTTP failure — never treat it as a legitimate empty result.

## Step 1 — Find the parent flow run

A sweep tags every one of its runs `run=<run_id>`: one parent plus one per engine point. Point runs are named by slug (`mns16_kvfp8_pcon`); the **parent is the run whose name is not a slug**.

```bash
curl -fsS -m 10 -X POST "$API/flow_runs/filter" -H 'Content-Type: application/json' \
  -d '{"flow_runs":{"tags":{"all_":["run=<run_id>"]}},"sort":"START_TIME_ASC","limit":200}'
```

Keep the runs whose `name` does not match `mns\d+_kv(fp8|fp16)_pc(on|off)`:

- **Zero** non-slug runs (or zero runs at all) → the `run_id` is wrong or the sweep never ran. Stop and say so.
- **Exactly one** → that is the parent. Keep its `id`.
- **More than one** → ambiguous (a retried or resumed parent). Stop and list the candidate names and ids for the user to disambiguate; do not pick one blind.

## Step 2 — Read the parent's artifacts

Keyed artifacts are versioned across runs: `/artifacts/latest/filter` returns a key's newest version from any run, so filtered to an older run it comes back empty. Read this run's own versions instead, newest first, and keep the first of each key:

```bash
curl -fsS -m 10 -X POST "$API/artifacts/filter" -H 'Content-Type: application/json' \
  -d '{"artifacts":{"flow_run_id":{"any_":["<parent-id>"]}},"sort":"CREATED_DESC","limit":100}'
```

Expect these five keys. Check they are all present before reading:

- `knob-sweep-config` — markdown embedding the run's `sweep-grid.yaml`. Read the SLO from its `goodput:` line, e.g. `goodput: ["ttft:1000", "tpot:50"]`. **This is the SLO you report for — never ask the user for one, and never infer one.** If the `goodput:` line is absent or does not parse into ttft/tpot thresholds, stop and report the config artifact is malformed — a report scoped to a guessed SLO is worse than none.
- `knob-sweep-ceiling-table` — markdown table, one row per `(max_num_seqs, kv_cache_dtype, prefix_caching, prefix_share)`, with columns `ceiling`, `p95_ttft_ms`, `p95_tpot_ms`, `output_throughput`. This is the ranking data.
- `knob-sweep-goodput-cliff` — markdown; read it to explain where ladders fell.
- `knob-sweep-ceiling-plot`, `knob-sweep-goodput-cliff-plot` — image artifacts whose `data` field is an S3 `https` URL to a PNG. The chart prefix (`knob-sweep/*/charts/*`) is public-read by bucket policy, so Step 4 downloads them with plain `curl`, no AWS credentials.

If `knob-sweep-config` or `knob-sweep-ceiling-table` is missing, stop and say which — the report cannot be built without them. If only a plot or the cliff is missing, continue but say in the report that it is degraded and which piece is absent.

The goodput floor each ceiling holds to is `0.95`, a fixed source constant (`packages/report/src/slipstream_bench/report/aggregation.py:28`) — state it, do not look for it per-run.

## Step 3 — Rank by throughput at the ceiling

The whole sweep runs on one fixed GPU, so `$/hr` is identical across configs and `$/1M-tokens ∝ 1 / output_throughput`. **Rank engine configs by `output_throughput` at the ceiling** — that ordering is the cost ordering.

Drop rows with a blank `ceiling` first — a point-and-share that held no rung within the SLO has no throughput-at-ceiling and cannot be ranked. Note them separately and use the cliff artifact to explain why they fell.

A server config is `(max_num_seqs, kv_cache_dtype, prefix_caching on/off)`. `prefix_share` is a property of the **workload**, not a knob you deploy. Rows differ by `prefix_share`, so do not pick the single highest-throughput row blind: decide which `prefix_share` bucket matches the target workload and rank configs within it. If the user has not said what their prefix-sharing looks like, ask, or report per bucket and say the pick depends on it.

If a `$/hr` was supplied, also compute `$/1M-tokens = ($/hr) / (output_throughput_tokens_per_s × 3600 / 1e6)` for the contenders.

## Step 4 — Write the report

The report is a trimmed slice of the tables plus the sweep's two plots, copied beside it. The results bucket is deleted on `stack-down` and the Prefect run ages out, so the report directory keeps its own copy of the plots and is committed to the repo.

The report directory is `reports/sweep-grid-<stamp>/`, where `<stamp>` is the `run_id` with any leading `sweep-full-` dropped (`sweep-full-20261008T130350Z` → `reports/sweep-grid-20261008T130350Z/`).

Fetch each plot to a `.part` file and keep it only if it is a whole PNG, so a timeout, a redirect body, or a dropped connection never leaves a corrupt image where git would commit it:

```bash
dir="reports/sweep-grid-<stamp>"
mkdir -p "$dir"
fetch_plot() {  # $1 = plot URL, $2 = file name in $dir/
  out="$dir/$2"
  rm -f "$out"
  if curl -fsS -m 30 -o "$out.part" "$1" \
    && [ "$(head -c 8 "$out.part" | xxd -p)" = "89504e470d0a1a0a" ]; then
    mv "$out.part" "$out"
  else
    rm -f "$out.part"
    echo "plot download failed: $1" >&2
    return 1
  fi
}
fetch_plot "<ceiling-plot-url>" ceiling-by-max-num-seqs.png
fetch_plot "<cliff-plot-url>" goodput-by-max-concurrency.png
```

The URLs are the image artifacts' `data` field. For a plot whose artifact was missing (Step 2), still remove any copy a previous run left (`rm -f` its file) and skip the fetch. A failed fetch is the same degradation as a missing artifact: the ranking does not depend on the plots, so continue and say in the report which plot is absent and the error `fetch_plot` printed.

Write `reports/sweep-grid-<stamp>/RECOMMENDATION.md` with these sections, in order:

1. **Run** — the `run_id` and the SLO honored (`ttft`/`tpot` thresholds, `0.95` floor).
2. **Pick** — one line: the recommended `(max_num_seqs, kv_cache_dtype, prefix_caching)` and the workload it is for.
3. **Ranking** — per `prefix_share` bucket, the top **N = 3** configs by `output_throughput` at the ceiling (N tunable at invocation), with their `ceiling`, `p95_ttft_ms`, `p95_tpot_ms`; add a `$/1M` column only if a price was given. This is the ceiling evidence.
4. **Tradeoff** — narrate the pick against the runner-up (e.g. "A beats B on throughput but B is within N% and sweeps a simpler knob → pick B"). Always name the runner-up and why it lost.
5. **Winner ladders** — from the cliff table, the rungs of each bucket's recommended config only — the one ladder whose fall through the 0.95 floor sets the ceiling. Not the full 240-row cliff; link the Prefect run page for the complete tables.
6. **Plots** — the two downloaded plots embedded by relative path (`![Concurrency ceiling by max_num_seqs](ceiling-by-max-num-seqs.png)`, `![Goodput cliff by max_concurrency](goodput-by-max-concurrency.png)`), with one line each on what they show. If a plot's artifact was missing (Step 2) or its fetch failed, say which and why instead of embedding.
7. **Caveats** — fixed single GPU (ranking is cost-exact only on this hardware), the SLO is the run's own, `0.95` floor.

Done when `RECOMMENDATION.md` exists with all seven sections and a pick justified against a named runner-up, and `reports/sweep-grid-<stamp>/` holds exactly the plots it embeds, with no `.part` file left.

## Out of scope

A bespoke winner-highlighted chart is a follow-up: it belongs as a tested plot function in the `report` package, not a throwaway plotting snippet here. This skill embeds the sweep's own plots only.
