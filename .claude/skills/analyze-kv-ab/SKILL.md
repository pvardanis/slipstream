---
name: analyze-kv-ab
description: Report whether KV cache offload paid off in a KV cache A/B run. Use when the user gives a `kv-cache-extension-ab` run_id or `run=` tag and wants its verdict, or a report on the A/B's spreads, ceilings, or hit rates.
---
<!-- Reads a completed KV cache A/B Prefect flow run over the REST API and writes a verdict report: whether KV cache offload paid off for the run's workload and SLO, rung by rung, with the evidence, the hit-rate mechanism check, the cost ratio and host-memory cost, a recommendation on turning offload on by default, and the run's chart. Prose-only: no repo code, no S3 sync, no report CLI. -->

# analyze-kv-ab

Given a KV cache A/B `run_id`, read its completed Prefect flow run and write a verdict: did turning KV cache offload on beat leaving it off, for this run's workload and SLO, and how sure can the run make us. The run already carries every number and the chart (ADR-0018); this skill reads them and reasons — it does not re-run the A/B, sync S3, or call the report CLI. It is the A/B's sibling of `analyze-knob-sweep` (ADR-0021).

Input: a `run_id` (e.g. `kv-ab-agent-20261023T090000Z`).

The two arms: **on** is vLLM with KV cache offload to LMCache's CPU tier enabled (`--kv-transfer-config` loads `LMCacheConnectorV1` in `kv_both` role, `packages/orchestration/src/slipstream_bench/orchestration/cluster.py:87`); **off** is vLLM with no KV connector. Both keep vLLM's GPU prefix cache. "Offload" in this skill and in the report always means KV cache offload to LMCache.

## Preconditions

Resolve the API base and check it the way `analyze-knob-sweep` does (its "Preconditions"): `API="${PREFECT_API_URL:-http://127.0.0.1:4200/api}"`, a `200` from `$API/health`, else stop and tell the user to run `just prefect-ui`. Every call uses `curl -fsS` with a timeout; any transport or HTTP error stops the skill and is reported as that error, never read as an empty result.

## Step 1 — Find the run

```bash
curl -fsS -m 10 -X POST "$API/flow_runs/filter" -H 'Content-Type: application/json' \
  -d '{"flows":{"name":{"any_":["kv-cache-extension-ab"]}},"flow_runs":{"tags":{"all_":["run=<run_id>"]}},"sort":"START_TIME_DESC","limit":50}'
```

The A/B has no child flow runs. More than one run under the tag means the run was resumed or re-run: the latest one re-renders from every rung, measured or resumed, so its tables are the whole run.

- **Zero** runs → the `run_id` is wrong or the A/B never ran. Stop and say so.
- **Latest run not `Completed`** → the A/B is in flight, failed, or was cancelled; its tables are missing or partial. Stop, give its state and name, and say a resume (same `run_id`) finishes it.
- Otherwise keep the latest run's `id`.

Done when you hold exactly one `Completed` run id.

## Step 2 — Read the run's artifacts

Keyed artifacts are versioned across runs: `/artifacts/latest/filter` returns a key's newest version from any run, so filtered to an older run it comes back empty. Read this run's own versions instead, newest first, and keep the first of each key; skip artifacts whose `key` is `null` (the flow's unkeyed progress notes):

```bash
curl -fsS -m 10 -X POST "$API/artifacts/filter" -H 'Content-Type: application/json' \
  -d '{"artifacts":{"flow_run_id":{"any_":["<run-id>"]}},"sort":"CREATED_DESC","limit":100}'
```

Expect four keys:

- `kv-ab-config` — the images, `model.yaml`, the workload YAML, and the vLLM args. Read from the workload YAML: the SLO (`load.goodput`, e.g. `["ttft:1000", "tpot:50"]`), `runs_per_arm`, `max_concurrency`, `prefix_share` (a percentage), `load.num_prefixes`, `load.total_len`, `load.output_len`, and `engine` (`max_num_seqs`, `kv_cache_dtype`, `kv_cache_memory_bytes`). **This is the SLO and workload you report for; never ask for or infer them.**

  **Why the GPU pool is capped.** When `engine.kv_cache_memory_bytes` is set in `kv-cache-extension-ab.yaml`, the run capped the GPU KV pool below the CPU tier on purpose (ADR-0023, "An eviction-sized workload on a capped GPU KV pool"). The g5.xlarge's host RAM cannot hold a CPU tier larger than the uncapped GPU pool. Without that, offload adds no reuse, because reachable reuse is max(GPU, CPU), not their sum. A larger tier needs the g5.2xlarge, which waits on a G-instance vCPU quota increase that has not been approved. Take the capped pool's token count from the YAML's comment on that key. When the key is absent, the pool was uncapped; say so, and drop the cap from the report.
- `kv-ab-rung-spreads` — one row per `(arm, max_concurrency)`: `runs`, then `goodput_fraction`, `p95_ttft_ms`, `p95_tpot_ms`, `output_throughput`, `gpu_hit_rate`, `combined_hit_rate`, each as `median [min–max]` across the arm's runs. This is the evidence.
- `kv-ab-ceilings` — one row per arm: each run's ceiling (`r1`, `r2`, …; `none` when no rung held the floor) and the median (blank when any run is `none`).
- `kv-ab-plot` — an image artifact whose `data` is a public `https` URL to the chart PNG.

**The CPU tier's host-memory cost.** The config artifact does not carry it; the orchestration image's tag does. That tag (`orch_image`, after the `:`) is the short hash of the last commit that touched the image's inputs, the vLLM manifest and the package source among them, so read the run's values from that commit:

```bash
git show <tag>:k8s/vllm-gpu.yaml | rg -A1 'LMCACHE_MAX_LOCAL_CPU_SIZE|LMCACHE_CHUNK_SIZE'
git show <tag>:packages/orchestration/src/slipstream_bench/orchestration/cluster.py | rg '_MEMORY_LIMIT_(OFFLOAD|DEFAULT) ='
```

`LMCACHE_MAX_LOCAL_CPU_SIZE` is the tier in GiB of pinned host memory; the two limits are the pod memory limit with offload on and off. A tag that is not a commit in the local clone (`git cat-file -t <tag>` fails) degrades the report: run `git fetch origin` once, and if it still fails, name the cost as unknown.

A missing `kv-ab-config` or `kv-ab-rung-spreads` stops the skill: say which. A missing ceilings table or plot degrades the report; continue and name the absent piece in it.

The goodput floor is `0.95`, a source constant (`packages/report/src/slipstream_bench/report/aggregation.py:28`).

Done when you hold the SLO, the workload fields above, the parsed spreads table, and the tier size and both pod memory limits (or know them to be unknown).

## Step 3 — Judge each rung

For every `max_concurrency` both arms measured, compare on against off on `goodput_fraction`, `p95_ttft_ms`, and `output_throughput`. A metric **separates** at a rung when the two arms' ranges do not overlap; the direction says which arm it favours (higher goodput and throughput, lower TTFT). A metric whose ranges overlap at a rung is **inconclusive** there; label it so, for `p95_tpot_ms` too, never as a win or a loss. Where `p95_tpot_ms` separates, say so beside the table; it informs the SLO attribution but not the verdict.

Separation is the strongest claim the run supports. With `n` runs per arm, complete separation in the direction you predicted has a one-sided permutation p of `1 / C(2n, n)`: `n=2` → 1/6, `n=3` → 1/20, `n=4` → 1/70. With `n=1` there are no ranges; every comparison is directional only.

The **verdict** is one of:

- **Offload wins** — at least one rung separates in on's favour and no rung separates in off's favour.
- **Offload loses** — the reverse.
- **No measurable difference** — no rung separates.
- **Mixed** — rungs separate in both directions; name which rungs favour which arm.

**Mechanism check**, from the hit rates:

- Off's `combined_hit_rate` should equal its `gpu_hit_rate`: off has no CPU tier. A gap means an off rung reached a connector; say so.
- On's `combined_hit_rate − gpu_hit_rate` is the share of prefix lookups the CPU tier served. A gap near zero means the tier served almost nothing, so any difference between the arms comes from something other than offload; say so.

**SLO attribution.** For an arm that misses the floor at a rung, name the gate it fails: `p95_ttft_ms` or `p95_tpot_ms` against the SLO's thresholds.

**Cost.** Both arms run on the same node at the same `$/hr`, so `$/1M tokens ∝ 1 / output_throughput`.

- Both median ceilings set → the on/off throughput ratio at each arm's ceiling rung is the cost ratio at the SLO.
- An arm's median ceiling blank → that arm held the SLO at no rung in every run: it cannot serve this SLO at the tested concurrency. Give the throughput ratio at the highest rung both arms measured as well, labelled as a ratio at that rung, not at the SLO.

**Host-memory cost.** Next to the cost ratio, state what offload costs in host RAM: the tier's GiB and the pod limit on against off (Step 2).

**Recommendation** on turning offload on by default, which is a follow-up change, not part of the run. Decide it from the verdict and the pool cap:

- **Offload loses, or no measurable difference** → keep it off: it costs the tier's host memory for no measured gain.
- **Offload wins on a capped pool** → keep it off on the node the run used. The default deployment runs the pool uncapped, and a tier smaller than that pool adds no reuse there (Step 2). Say what would change it: a node whose RAM fits a tier larger than the uncapped pool, and a re-run of the A/B there.
- **Offload wins on an uncapped pool** → turn it on for workloads like this run's, weighed against the host-memory cost.
- **Mixed** → keep it off, and name the rungs where it lost.

Done when every shared rung has a separation or inconclusive call per metric, the verdict follows from them, and the SLO attribution, mechanism check, cost comparison, host-memory cost, and recommendation are stated.

## Step 4 — Write the report

The report outlives the results bucket and the Prefect run, so it keeps its own copy of the chart. Fetch it to a `.part` file and keep it only if it is a whole PNG:

```bash
dir="reports/kv-cache-ab-<run_id>"; out="$dir/kv-ab-by-max-concurrency.png"
mkdir -p "$dir"; rm -f "$out"
if curl -fsS -m 30 -o "$out.part" "<kv-ab-plot data URL>" \
  && [ "$(head -c 8 "$out.part" | xxd -p)" = "89504e470d0a1a0a" ]; then
  mv "$out.part" "$out"
else
  rm -f "$out.part"; echo "chart download failed" >&2
fi
```

With no `kv-ab-plot` artifact, run only the `mkdir` and `rm -f` lines. A missing artifact or failed fetch degrades the report as in Step 2.

Write `reports/kv-cache-ab-<run_id>/RECOMMENDATION.md` with these sections, in order:

1. **Run** — the `run_id`, what each arm is (on: KV cache offload to LMCache's CPU tier enabled; off: GPU prefix cache only), the SLO (`ttft`/`tpot`, `0.95` floor), `runs_per_arm` and their interleaved order (off-r1, on-r1, off-r2, …; each run a redeploy, warmup, then the ladder), the rungs, the workload (`prefix_share`, `num_prefixes`, `total_len`, `output_len`), the engine point, the CPU tier (GiB and chunk size, Step 2), and the GPU pool cap (`kv_cache_memory_bytes`, in bytes and tokens) with why it is capped (Step 2), plus the three image refs from the config artifact, with the AWS account ID in an ECR registry host replaced by `<account>` (`<account>.dkr.ecr.<region>.amazonaws.com/...`).
2. **Verdict** — one line: the verdict, naming LMCache KV cache offload, the rungs it rests on, and the workload it holds for.
3. **Evidence** — a table per rung: off and on `median [range]` for `goodput_fraction`, `p95_ttft_ms`, `p95_tpot_ms`, `output_throughput`, and a column naming the judged metrics that separate, with an **inconclusive** row naming every metric whose ranges overlap (`none` when none do). Under it, the SLO attribution. State the permutation p for this run's `n`.
4. **Mechanism** — both hit rates per arm as `median [min–max]`, the CPU tier's share (on's combined median minus its GPU median; the spreads table does not pair values by run, so it has no range), and the mechanism check's conclusion.
5. **Ceilings and cost** — the ceilings table, the cost comparison from Step 3, and the host-memory cost beside it.
6. **Recommendation** — whether to turn offload on by default, with the reasoning from Step 3.
7. **Chart** — the downloaded PNG embedded by relative path (`![KV cache A/B by max_concurrency](kv-ab-by-max-concurrency.png)`) with one line on what it shows, or the reason it is absent.
8. **Caveats** — one fixed GPU, so the cost ratio holds on this hardware only; the SLO and workload are the run's own; when `kv_cache_memory_bytes` is set, the run capped the GPU KV pool at `kv_cache_memory_bytes` because the CPU tier the node's RAM allows is smaller than the uncapped pool, pending the g5.2xlarge quota (Step 2). The flow refuses a pool that holds the working set, so the result describes a working set larger than the GPU pool, not an uncapped engine; on the uncapped g5.xlarge, offload adds no reuse (ADR-0023, Consequences). Say this beside every cost ratio the report quotes; the rungs measured bound the ceilings (a ceiling at the top rung may sit higher); the significance bound from Step 3.

Done when `RECOMMENDATION.md` has all eight sections, the verdict traces to named rungs and metrics, and `reports/kv-cache-ab-<run_id>/` holds exactly the chart it embeds, with no `.part` file left.
