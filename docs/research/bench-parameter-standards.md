<!-- Research note: parameter values used by standard LLM inference serving benchmarks, used to sanity-check slipstream's own bench defaults. -->

# LLM serving benchmark parameter standards

Reference values from four standard LLM inference serving benchmarks, gathered to
sanity-check slipstream's grid-cell defaults. Every claim below cites a primary
source (source code, official docs, or the MLPerf rules spec).

## Our defaults under review

| Knob | Our value |
| --- | --- |
| `num_prompts` (requests per grid cell) | 100 |
| `num_prefixes` (distinct shared prefixes) | 5 |
| `output_len` | 128 tokens |
| `request_rate` | 8 req/s |
| goodput SLO | TTFT p95 ≤ 1000 ms, TPOT ≤ 50 ms |

---

## 1. vLLM `vllm bench serve` / `benchmarks/benchmark_serving.py`

vLLM is the closest analogue to our harness — it is literally the same CLI family,
and our `prefix_repetition` knobs are named after its dataset.

**Argparse hard-coded defaults** (source: `vllm/benchmarks/datasets/datasets.py`,
`add_dataset_parser`):

| Flag | Default |
| --- | --- |
| `--num-prompts` | `1000` (`DEFAULT_NUM_PROMPTS`) |
| `--random-input-len` | `1024` |
| `--random-output-len` | `128` |
| `--random-prefix-len` | `0` |
| `--random-range-ratio` | `0.0` |
| `--sharegpt-output-len` | `None` (use dataset's own length) |
| `--prefix-repetition-prefix-len` | `256` |
| `--prefix-repetition-suffix-len` | `256` |
| `--prefix-repetition-num-prefixes` | `10` |
| `--prefix-repetition-output-len` | `128` |

Source: [vllm/benchmarks/datasets/datasets.py (main)](https://github.com/vllm-project/vllm/blob/main/vllm/benchmarks/datasets/datasets.py) — dataset argument parser and default constants.

**Load / rate defaults** (source: `vllm/benchmarks/serve.py`, `add_cli_args`):

- `--request-rate` default = `inf` (all requests fired at t=0, i.e. max load / a
  saturation test, not a fixed arrival rate).
- `--burstiness` default = `1.0` (Poisson arrivals when a finite rate is set).
- `--metric-percentiles` default = `"99"`; `--percentile-metrics` default =
  `"ttft,tpot,itl"` for generation.
- `--goodput` has no default; it is opt-in as `KEY:VALUE` ms pairs over
  `ttft`, `tpot`, `e2el`.

Source: [vllm/benchmarks/serve.py (main)](https://github.com/vllm-project/vllm/blob/main/vllm/benchmarks/serve.py) — serving benchmark CLI defaults.

**Documented `prefix_repetition` example** (source: vLLM benchmarking CLI docs):

```bash
vllm bench serve --dataset-name prefix_repetition \
  --num-prompts 100 \
  --prefix-repetition-prefix-len 512 \
  --prefix-repetition-suffix-len 128 \
  --prefix-repetition-num-prefixes 5 \
  --prefix-repetition-output-len 128
```

Source: [docs/benchmarking/cli.md (main)](https://github.com/vllm-project/vllm/blob/main/docs/benchmarking/cli.md) and the rendered [Benchmark CLI page](https://docs.vllm.ai/en/latest/benchmarking/cli/).

**Note:** Our `num_prompts=100`, `num_prefixes=5`, `output_len=128` are exactly the
values in vLLM's own documented `prefix_repetition` example. So against vLLM's
*published example* we are dead-on; against its *argparse defaults* we run fewer
prompts (100 vs 1000) and fewer prefixes (5 vs 10).

---

## 2. MLPerf Inference (MLCommons) — LLM benchmarks

MLPerf is the strict, audited standard. It reports the **max QPS that still meets a
p99 latency SLO**, so it is the authority on latency thresholds and sample counts.

**Server-scenario latency constraints** (TTFT / TPOT), from the inference rules:

| Benchmark | Conversational (Server) | Interactive |
| --- | --- | --- |
| Llama2-70B (QA) | TTFT 2000 ms / TPOT 200 ms | TTFT 450 ms / TPOT 40 ms |
| Llama3.1-8B (summarization) | TTFT 2000 ms / TPOT 100 ms | TTFT 500 ms / TPOT 30 ms |
| Llama3.1-405B (text gen) | TTFT 6000 ms / TPOT 175 ms | TTFT 4500 ms / TPOT 80 ms |
| Mixtral-8x7B | TTFT 2000 ms / TPOT 200 ms | — |
| DeepSeek-R1 (reasoning) | TTFT 2000 ms / TPOT 80 ms | TTFT 1500 ms / TPOT 15 ms |
| GPT-OSS-120B | TTFT 3000 ms / TPOT 80 ms | TTFT 2000 ms / TPOT 20 ms |

Constraints are enforced at the **99th percentile**. Source: [mlcommons/inference_policies — inference_rules.adoc](https://github.com/mlcommons/inference_policies/blob/master/inference_rules.adoc) (LLM latency-constraint table) and [Llama2-70B README](https://github.com/mlcommons/inference/blob/master/language/llama2-70b/README.md) (interactive TTFT 450 ms / TPOT 40 ms).

**Sample / query counts** (QSL performance sample count):

| Benchmark | Performance sample count |
| --- | --- |
| Llama2-70B | 24,576 |
| Llama3.1-8B | 13,368 |
| Llama3.1-405B | 8,313 |
| Mixtral-8x7B | 15,000 |
| DeepSeek-R1 | 4,388 |
| GPT-OSS-120B | 6,396 |

**Run-length rules:** minimum benchmark duration **600 s** for all scenarios except
Offline; the Server scenario runs many thousands of queries (governed by the
600 s minimum plus an early-stopping rule that guarantees the 99th-percentile
estimate is statistically valid). Source: [inference_rules.adoc](https://github.com/mlcommons/inference_policies/blob/master/inference_rules.adoc) (min-duration and early-stopping sections; per-benchmark QSL sizes).

**Note:** MLPerf never validates a tail percentile on ~100 samples. Its whole
early-stopping machinery exists because a p99 needs thousands of queries to be
stable. Its output length is dataset-driven (~294 tokens/sample average for
Llama2-70B), longer than our fixed 128.

---

## 3. NVIDIA GenAI-Perf (Triton `perf_analyzer` / genai-perf)

**Synthetic prompt + load defaults** (source: genai-perf README):

| Flag | Default |
| --- | --- |
| `--synthetic-input-tokens-mean` | 550 |
| `--synthetic-input-tokens-stddev` | 0 |
| `--output-tokens-mean` | -1 (unset; model decides) |
| `--output-tokens-stddev` | 0 |
| `--num-dataset-entries` | 100 unique payloads |
| `--request-count` | 10 |
| `--warmup-request-count` | 0 |
| `--num-prefix-prompts` | 0 (prefix-cache workload off by default) |
| `--concurrency` | None |
| `--request-rate` | None |

Source: [triton-inference-server/perf_analyzer — genai-perf/README.md](https://github.com/triton-inference-server/perf_analyzer/blob/main/genai-perf/README.md).

**Underlying perf_analyzer stability defaults** (what actually governs sample size):

- `--measurement-mode` default `time_windows`; `--measurement-interval` default
  **5000 ms**. With the default stability logic GenAI-Perf benchmarks for
  ~3× the measurement interval per load point.
- `--measurement-request-count` (count-window mode) default **50** requests/window.
- `--concurrency` sweeps: load is generated by holding a fixed concurrency (or a
  fixed request-rate); you sweep the concurrency list to build the latency/throughput
  curve.

Source: [Perf Analyzer CLI reference](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/perf_analyzer/docs/cli.html) and [GenAI-Perf docs](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/perf_analyzer/genai-perf/README.html). NVIDIA's benchmarking blog recommends raising `--measurement-interval` (e.g. 30000–100000 ms) for large models / high concurrency so enough requests finish per window: [LLM Inference Benchmarking Guide](https://developer.nvidia.com/blog/llm-performance-benchmarking-measuring-nvidia-nim-performance-with-genai-perf/).

**Note:** GenAI-Perf's `--request-count 10` default is tiny (it is a smoke default);
real runs are driven by the time-window stability loop, not a fixed request count.
It has no built-in TTFT/TPOT SLO — it reports the percentiles, it does not gate on
them. genai-perf is being deprecated in favour of NVIDIA AIPerf.

---

## 4. LLMPerf (ray-project/llmperf)

**`token_benchmark_ray.py` argparse defaults** (source: script):

| Flag | Default |
| --- | --- |
| `--num-concurrent-requests` | 10 |
| `--max-num-completed-requests` | 10 |
| `--mean-input-tokens` | 550 |
| `--stddev-input-tokens` | 150 |
| `--mean-output-tokens` | 150 |
| `--stddev-output-tokens` | 80 |
| `--timeout` | 90 s |

Source: [ray-project/llmperf — token_benchmark_ray.py](https://github.com/ray-project/llmperf/blob/main/token_benchmark_ray.py).

**README example values:** `--mean-input-tokens 550 --stddev-input-tokens 150
--mean-output-tokens 150 --stddev-output-tokens 10 --max-num-completed-requests 2
--num-concurrent-requests 1` (a low smoke-test example). Source:
[ray-project/llmperf — README.md](https://github.com/ray-project/llmperf/blob/main/README.md).

**Load model:** fixed **concurrency**, not request rate — N worker threads each send,
wait, and repeat. You raise concurrency to raise load; there is no arrival-rate knob
and no prefix-cache / shared-prefix dataset. No built-in TTFT/TPOT SLO gate.

---

## Where our five defaults land

| Knob | Our value | Verdict vs norms |
| --- | --- | --- |
| `num_prompts = 100` | requests/cell | **Low for a stable p95/p99.** Matches vLLM's *example* (100) but is 1/10 of vLLM's argparse default (1000) and orders of magnitude below MLPerf's thousands. A p95 on 100 samples is the 5th-worst request; a p99 is barely defined. Fine for a p50/mean or a rough p95 trend; not defensible for a hard p99 gate. |
| `num_prefixes = 5` | distinct prefixes | **Reasonable / on-spec.** Exactly vLLM's documented `prefix_repetition` example; half of vLLM's argparse default (10). Low prefix count = high cache reuse, which is the intended stress for a prefix-cache workload. |
| `output_len = 128` | tokens | **Reasonable, on the short side.** Equals vLLM's `--random-output-len` and `--prefix-repetition-output-len` defaults (128) and GenAI-Perf's neighbourhood, but shorter than LLMPerf (150) and much shorter than MLPerf's dataset-driven ~294. Short outputs mean fewer decode steps, so TPOT is averaged over a short tail — acceptable but don't over-read TPOT stability. |
| `request_rate = 8 req/s` | fixed | **Reasonable as a single operating point, but note nobody else fixes a single rate.** vLLM defaults to `inf` (saturation); GenAI-Perf and LLMPerf sweep concurrency; MLPerf searches for max QPS under the SLO. A single 8 req/s point gives one slice of the latency-vs-load curve; the standard practice is to sweep rate/concurrency and report the curve or the max sustainable rate. |
| SLO: TTFT p95 ≤ 1000 ms, TPOT ≤ 50 ms | goodput gate | **In a sane band, and stricter on TPOT than MLPerf's conversational tier.** MLPerf conversational TTFT is 2000 ms (ours 1000 is tighter); MLPerf interactive TTFT is 450–500 ms (ours is looser). Our TPOT 50 ms sits between MLPerf interactive (30–40 ms) and conversational (100–200 ms) — a reasonable "interactive-ish" target. Two caveats: (a) we gate on **p95**, whereas MLPerf gates on **p99** — p95 is the more forgiving and the more estimable at low N; (b) our TPOT gate reads as a mean/threshold, not a percentile — confirm which we compute. |

### Headline

Our per-knob values are individually sane and clearly modelled on vLLM's published
`prefix_repetition` example (num_prompts 100 / num_prefixes 5 / output_len 128 line
up one-for-one). The one real weakness is **sample size**: `num_prompts = 100` is too
small for a trustworthy p95, and far too small for any p99, by the standard every
serious benchmark sets — vLLM defaults to 1000, and MLPerf runs thousands precisely
because tail percentiles need them. If the p95 TTFT / TPOT gate is load-bearing,
raise requests-per-cell (≥500–1000) or relax the reported statistic to p50/mean with
a p95 shown only as a trend. Fixing a single 8 req/s point (rather than sweeping
rate/concurrency like every tool here) is a deliberate simplification worth stating
explicitly, since it captures one slice of the load curve rather than the curve.
