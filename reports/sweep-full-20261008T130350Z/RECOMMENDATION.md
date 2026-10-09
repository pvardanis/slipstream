# Server-config recommendation — `sweep-full-20261008T130350Z`

## Run

- **run_id**: `sweep-full-20261008T130350Z` (Prefect parent run `fair-groundhog`, `b2f5bc1c`; tag `run=sweep-full-20261008T130350Z`)
- **SLO honored** (read from the run's `knob-sweep-config`): p95 **TTFT ≤ 1000 ms**, p95 **TPOT ≤ 50 ms**, goodput **floor 0.95**.
- Model on one fixed A10G, `--tensor-parallel-size 1`. Every config runs at the same `$/hr`, so ranking by `output_throughput` at the ceiling *is* the `$/token` ranking.

At each config's ceiling the latency gates still pass with headroom (p95 TTFT ≤ 734 ms, p95 TPOT ≤ 42 ms); the ceiling is set by the 0.95 goodput floor, not a gate breach — the fall happens at the next concurrency step (see Winner ladders). So the decision is throughput, and **`kv_cache_dtype = fp8` tops every bucket** — it never loses on the gates and frees KV memory. Recommend fp8.

**The dominant lever is the workload, not the engine knob.** Prefix sharing raises both the sustainable concurrency ceiling (8 → 32) and throughput (~255 → ~1140 tok/s) by ~4.5×, while within a bucket `max_num_seqs` barely moves throughput and leaves the ceiling unchanged. Match the config to your workload's prefix sharing first.

## Pick

`prefix_share` is a load property, not a server knob, so the pick is per workload bucket (all fp8, all prefix-caching on except the no-sharing baseline):

| workload (prefix_share) | pick | tok/s @ ceiling | ceiling |
| --- | --- | --- | --- |
| 0% | `32 fp8 off` | 255.2 | 8 |
| 10% | `64 fp8 on` | 269.5 | 8 |
| 50% | `64 fp8 on` | 476.4 | 16 |
| 90% | `32 fp8 on` | 1140.3 | 32 |

If you tell me your real prefix-sharing, I'll collapse this to one line.

## Ranking (top 3 per bucket by throughput at ceiling)

**prefix_share 0%**

| config | tok/s | ceiling | p95 ttft (ms) | p95 tpot (ms) |
| --- | --- | --- | --- | --- |
| `32 fp8 off` | 255.2 | 8 | 505 | 34 |
| `64 fp8 off` | 255.2 | 8 | 716 | 35 |
| `256 fp8 off` | 253.5 | 8 | 712 | 34 |

**prefix_share 10%**

| config | tok/s | ceiling | p95 ttft (ms) | p95 tpot (ms) |
| --- | --- | --- | --- | --- |
| `64 fp8 on` | 269.5 | 8 | 491 | 31 |
| `128 fp8 on` | 264.4 | 8 | 485 | 32 |
| `16 fp8 on` | 264.1 | 8 | 471 | 33 |

**prefix_share 50%**

| config | tok/s | ceiling | p95 ttft (ms) | p95 tpot (ms) |
| --- | --- | --- | --- | --- |
| `64 fp8 on` | 476.4 | 16 | 519 | 35 |
| `128 fp8 on` | 475.9 | 16 | 633 | 36 |
| `32 fp8 on` | 472.0 | 16 | 622 | 36 |

**prefix_share 90%**

| config | tok/s | ceiling | p95 ttft (ms) | p95 tpot (ms) |
| --- | --- | --- | --- | --- |
| `32 fp8 on` | 1140.3 | 32 | 443 | 37 |
| `128 fp8 on` | 1138.7 | 32 | 292 | 42 |
| `256 fp8 on` | 1129.3 | 32 | 339 | 35 |

## Tradeoff

Within most buckets the throughput leader also wins latency and ties on ceiling, so the pick dominates rather than trades off:

- **share 0%** — `32 fp8 off` and `64 fp8 off` tie on throughput (255.2) and ceiling (8); `32` wins on p95 TTFT (505 vs 716 ms) and is the smaller batch, so it is the pick with nothing given up.
- **share 50%** — `64 fp8 on` (476.4) edges `128 fp8 on` (475.9) on throughput at the same ceiling (16) *and* has the lower TTFT (519 vs 633 ms), so no tradeoff — pick `64 fp8 on`.
- **share 90%** — the one real tradeoff. `32 fp8 on` leads on throughput (1140.3 vs 1138.7, +0.1%) and TPOT (37 vs 42 ms) at the same ceiling (32), but `128 fp8 on` has markedly better TTFT (292 vs 443 ms). For throughput/cost they are a wash; if first-token latency matters (interactive serving) prefer `128 fp8 on`, otherwise `32 fp8 on` for the smaller batch.

## Winner ladders

The cliff rungs for each bucket's pick — the ladder whose fall through the 0.95 floor sets the ceiling. The complete ceiling and cliff tables (all 40 configs, 240 rungs) stay on the Prefect run `fair-groundhog` (tag `run=sweep-full-20261008T130350Z`).

**share 0% — `32 fp8 off`**

| max_concurrency | goodput_fraction | p95 ttft (ms) | p95 tpot (ms) | tok/s |
| --- | --- | --- | --- | --- |
| 8 | 0.984 | 505 | 34 | 255.2 |
| 16 | 0.755 | 961 | 55 | 315.2 |
| 32 | 0.085 | 1444 | 102 | 354.1 |
| 64 | 0.000 | 11090 | 95 | 360.0 |
| 128 | 0.000 | 30833 | 97 | 357.3 |
| 256 | 0.000 | 69765 | 109 | 348.3 |

**share 10% — `64 fp8 on`**

| max_concurrency | goodput_fraction | p95 ttft (ms) | p95 tpot (ms) | tok/s |
| --- | --- | --- | --- | --- |
| 8 | 0.979 | 491 | 31 | 269.5 |
| 16 | 0.824 | 1100 | 54 | 333.6 |
| 32 | 0.089 | 1463 | 112 | 380.5 |
| 64 | 0.051 | 4152 | 229 | 399.3 |
| 128 | 0.000 | 21859 | 231 | 399.3 |
| 256 | 0.000 | 54154 | 249 | 394.5 |

**share 50% — `64 fp8 on`**

| max_concurrency | goodput_fraction | p95 ttft (ms) | p95 tpot (ms) | tok/s |
| --- | --- | --- | --- | --- |
| 8 | 0.979 | 303 | 23 | 350.6 |
| 16 | 0.953 | 519 | 35 | 476.4 |
| 32 | 0.288 | 1441 | 75 | 586.1 |
| 64 | 0.056 | 2164 | 120 | 641.8 |
| 128 | 0.000 | 13453 | 128 | 643.7 |
| 256 | 0.000 | 35943 | 130 | 636.2 |

**share 90% — `32 fp8 on`**

| max_concurrency | goodput_fraction | p95 ttft (ms) | p95 tpot (ms) | tok/s |
| --- | --- | --- | --- | --- |
| 8 | 0.995 | 116 | 16 | 496.9 |
| 16 | 0.996 | 155 | 23 | 802.8 |
| 32 | 0.968 | 443 | 37 | 1140.3 |
| 64 | 0.033 | 3422 | 37 | 1140.4 |
| 128 | 0.032 | 9097 | 37 | 1147.8 |
| 256 | 0.012 | 19991 | 39 | 1149.3 |

## Plots

Linked to the public S3 URIs (from the run's Prefect image artifacts), not embedded.

- [Concurrency ceiling by max_num_seqs](https://slipstream-bench-endpoint-results-b9dd63990b80a78fe468e97eef.s3.eu-west-1.amazonaws.com/sweeps/sweep-full-20261008T130350Z/charts/ceiling-by-max-num-seqs.png) — sustained `--max-concurrency` holding goodput ≥ 0.95 per engine point; which configs hold the most load.
- [Goodput cliff by max_concurrency](https://slipstream-bench-endpoint-results-b9dd63990b80a78fe468e97eef.s3.eu-west-1.amazonaws.com/sweeps/sweep-full-20261008T130350Z/charts/goodput-by-max-concurrency.png) — where each ladder fell through the 0.95 floor, and which gate (TTFT vs TPOT) bit at the edge.

## Caveats

- **Fixed single GPU.** Throughput-ranking equals cost-ranking only while every config shares one GPU type at one `$/hr`. A multi-GPU or mixed-hardware sweep breaks this and needs real per-config cost.
- **SLO is the run's own** (TTFT ≤ 1000, TPOT ≤ 50, floor 0.95). A different SLO means a new sweep.
- **No price supplied**, so no absolute `$/1M-tokens`. Pass a `$/hr` to get it; it would not change the ordering.
- **Plots** are linked to the public `sweeps/*/charts/*` S3 prefix; they render while the object lives.
