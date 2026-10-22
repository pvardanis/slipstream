<!-- ADR recording the KV cache extension experiment (#34): a CPU-only LMCache tier on the g5.xlarge with a 12Gi offload memory limit, LMCache loaded through vLLM's V1 KV connector on the stock vllm/vllm-openai:v0.29.0 image (which already ships LMCache 0.5.4), an off-by-default offload switch, a separate kv-cache-extension-ab flow instead of a knob-sweep axis, a capped GPU KV pool and eviction-sized workloads behind fit guards, and 3 interleaved runs per arm. States the facts #325's live smoke run verified. -->

# ADR-0023: KV cache extension through a CPU tier, measured by a separate A/B

- Status: Accepted
- Date: 2026-10-22

## Context

The GPU replica keeps its prefix cache only in GPU memory. Once the working set of distinct
prefixes outgrows the GPU KV pool, vLLM evicts least recently used blocks, and the next
request carrying an evicted prefix pays its whole prefill again. LMCache can copy KV cache
into a CPU-memory tier and load it back instead. The KV cache extension experiment (#34)
asks whether that win is real on this rig, how large it is, and what it costs in host
memory. L4 (prefill/decode disaggregation) reuses the same LMCache setup, so it lands and is
measured at L1 first.

The spec for #34 sized the experiment from estimates. #325's smoke run on the live replica
replaced them with the facts below, and two of those facts changed the design: the stock
image already ships LMCache (#324), and the CPU tier the g5.xlarge can afford is smaller
than its GPU KV pool.

The knowledge base's `vllm-internals.md` covers the KV connector hooks and block-aligned
prefix caching (§2, §4), but not CPU offload or LMCache's configuration. The facts here come
from #324, #325 and #326, and from the vLLM v0.29.0 and LMCache v0.5.4 sources those
tickets cite.

### Facts verified in #325's smoke run

On a g5.xlarge with vLLM 0.29.0, LMCache 0.5.4-g3e11b8ed, Qwen3-8B-AWQ, fp8 KV, max-model-len
4096 and max-num-seqs 64:

| Fact | Verified value |
|---|---|
| GPU KV pool, uncapped, from the startup log | 185,696 tokens, the same with offload on and off |
| CPU tier | 4 GiB of pinned host memory in 256-token chunks, LRU |
| fp8 KV cache dtype | supported: LMCache stores it as `torch.uint8`, 73,728 bytes per token |
| CPU tier capacity in tokens | 4 GiB / 73,728 bytes ≈ 58,254 tokens |
| Host RSS, idle | EngineCore 6.5 GiB, APIServer 1.6 GiB |
| Pod cgroup, idle, offload on | 9.6 of 12 GiB: 2.9 GiB anon, 4.1 GiB shmem (the tier), the rest reclaimable page cache |
| memlock in the pod | unlimited |
| LMCache in the stock image | 0.5.4 |

Two behaviours the smoke run showed, and one counter semantic #326 read from the sources,
also shape the design:

- LMCache copies each prefill's KV to the CPU tier when the prefill runs, not when the GPU
  evicts it. A tail shorter than one chunk is not stored (`save_unfull_chunk: False`).
- With a GPU pool capped at 16,384 tokens, a ~2.3k-token prefix evicted by eight other
  prompts reloaded from the tier with 0 tokens recomputed. The request took 0.40 s against
  0.87 s for the first send and 0.96 s for a new prompt, and the reload ran at 20.4 GB/s.
- vLLM counts LMCache hits in `vllm:external_prefix_cache_hits` and
  `vllm:external_prefix_cache_queries`, not in `vllm:prefix_cache_*`. External hits are the
  tokens LMCache actually loads, so a prefix still on the GPU counts 0 (#326).

## Decision

### A CPU-only tier on the g5.xlarge, with a 12Gi memory limit when offload is on

The tier is 4 GiB of pinned CPU memory (`LMCACHE_MAX_LOCAL_CPU_SIZE: "4.0"`) in 256-token
chunks. With offload on, the pod memory limit rises from 8Gi to 12Gi: the 8Gi the engine
needs plus the tier. The smoke run measured the pod at 9.6 GiB idle under that limit, and an
unlimited memlock, so the pinned allocation does not fail.

Alternatives considered:

- **A disk or NVMe tier.** It would need instance-store setup on the node pool, and it is
  slower than host RAM. It is out of scope for #34.
- **A larger CPU tier on the g5.xlarge.** The node has 14.4 GiB allocatable. The engine
  takes about 8 GiB idle, so a tier larger than the GPU pool (185,696 tokens is about
  12.8 GiB) does not fit.
- **A g5.2xlarge (32 GiB host RAM).** It would fit a ~16 GiB tier (~233k tokens) under a
  ~24Gi limit, which exceeds the uncapped GPU pool. It needs a G-instance vCPU quota
  increase to 8 in eu-west-1. Moving to it is a follow-up; the
  flow reads the tier size from the manifest, so its code does not change when the rig does.

### LMCache loads through vLLM's V1 KV connector

With offload on, the manifest renders

```text
--kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'
```

and LMCache reads its tier from `LMCACHE_LOCAL_CPU`, `LMCACHE_MAX_LOCAL_CPU_SIZE` and
`LMCACHE_CHUNK_SIZE` in the container env. `kv_both` lets the one replica both store and
load KV. LMCache runs inside the engine process, so no other process or service is
deployed. vLLM's own prefix caching stays on in both states: the experiment measures the
extension beyond the GPU tier, not a replacement for it.

Alternatives considered:

- **`--kv-offloading-backend lmcache`.** vLLM's shortcut only acts when
  `--kv-offloading-size` is also set. Its `lmcache` backend then selects
  `LMCacheMPConnector`, which talks to a standalone `lmcache server` process. LMCache's
  server mode is out of scope for #34.
- **vLLM's native CPU offload (`--kv-offloading-backend native`).** It selects vLLM's own
  offloading connector rather than LMCache, so L4 would not inherit the setup this measures.

### Offload runs on the stock image, not a derived one

The spec planned a derived image: the stock `vllm/vllm-openai:v0.29.0` plus a pinned LMCache,
with a build check that vLLM and torch stayed the same. #324 found the stock image already
has LMCache. Its release build ran with `INSTALL_KV_CONNECTORS=true`, and the layer that
installs `requirements/kv_connectors.txt` adds LMCache 0.5.4, nixl 1.3.2 and
mooncake-transfer-engine 0.3.13.post1, with no torch or vLLM dist-info. LMCache 0.5.4 builds
against `torch==2.13.0`, the torch vLLM 0.29.0 pins. The smoke run then confirmed that 0.5.4
supports the fp8 KV cache dtype.

So both arms run the same image, and the only variable between them is the offload switch.
`k8s/vllm-gpu.yaml` stays the single source of the engine image. A derived image would have
added a build, a CI workflow, a scan and a stock-against-derived smoke run, all to produce an
engine the stock image already is. It can be revisited if a later need outgrows LMCache 0.5.4.

### One switch, off by default

`just _render-gpu-manifest` reads `KV_CACHE_OFFLOAD`, which is `off` unless set to `on`; any
other value fails the render. The worker's render takes the same switch as a boolean: the
knob sweep passes off, and the A/B passes each arm's value. On, a render sets the
connector config and the 12Gi limit together, so a half-configured offload cannot be
deployed. Off, it renders `--kv-transfer-config None`, which vLLM parses as no connector, and
the 8Gi limit. The `LMCACHE_*` env is in the manifest either way; nothing reads it unless
the connector loads LMCache.

`gpu-deploy` and the knob sweep therefore run with offload off. Whether to turn it on by
default is a separate decision, made on the A/B's numbers.

Alternatives considered:

- **On by default.** It would change every existing deployment and knob-sweep measurement
  before there is evidence that it helps.
- **A second manifest for offload.** The two manifests could drift apart on everything else,
  which is what the A/B must hold equal.

### A separate A/B flow, not a knob-sweep axis

The `kv-cache-extension-ab` flow (`kv-ab` for short) compares offload off against on at one
engine point. It follows the knob sweep's structure: a transport-free driver with injected
collaborators, inside a thin Prefect flow. It reuses the cell runner and S3 storage, and
renders its own tables and chart to the run page (ADR-0018). Its workload config lives in
`bench/configs/kv-cache-extension-ab.yaml`. Its records go under their own prefix, labelled
with arm and run, and its report has its own skill, `analyze-kv-ab` (ADR-0021).

Alternatives considered:

- **An offload axis in the knob-sweep grid.** It would change every point slug, cache key
  and config digest, and analyze-bench's parsing (ADR-0022). The grid also has no notion of
  repeated, interleaved runs, a warmup, an eviction-sized workload or the connector's hit
  counters, which this comparison needs and the knob sweep does not.

### An eviction-sized workload on a capped GPU KV pool, behind fit guards

On the smoke rig the CPU tier (~58k tokens) is smaller than the GPU pool (185,696 tokens).
LMCache copies every prefill to the tier, and both tiers evict least recently used first, so
any prefix the GPU pool has dropped is already gone from the tier. Offload would then add
copy overhead and no reuse: reachable reuse is max(GPU, CPU), not GPU + CPU.

So each arm caps the GPU pool with `--kv-cache-memory-bytes 1207959552`, which is 16,384 fp8
tokens. That puts the pool below the tier. A plain `gpu-deploy` and the knob sweep stay
uncapped. vLLM's `prefix_repetition` dataset then runs workloads whose distinct prefixes
overflow the pool but whose prompts fit the tier. Each workload has 3584 prompt tokens and
128 output tokens on a `[1, 2, 4]` concurrency ladder. Each prefix is its share of the
prompt floored to whole 256-token chunks, so the agent's 90% share is a 3072-token prefix:

| Workload | Prefix share | Prefixes × length | Working set | Tier demand |
|---|---|---|---|---|
| agent (the live config) | 90% | 8 × 3072 | 24,576 tokens, 1.5× the pool | 28,672 tokens |
| chat | 50% | 12 × 1792 | 21,504 tokens, 1.3× the pool | 43,008 tokens |

The prefix count and length are fixed in the config, not computed at runtime, so runs are
reproducible.

The guards refuse a workload that cannot show the effect, before a GPU minute is spent. The
rig's max-model-len, tier size and chunk size are read off `k8s/vllm-gpu.yaml`. Before
deploy, the flow refuses a workload when:

- a request runs past max-model-len;
- a prefix is not a whole number of chunks, since the last partial chunk is never reused;
- the prompts, rounded up to whole chunks, overflow the tier. LMCache stores every chunk of
  every prompt, so the unique tails share the tier with the prefixes;
- the pool holds the whole working set, so offload has nothing to serve;
- the pool cannot hold the top rung's requests, so both arms would measure preemption.

After each redeploy, the driver checks the last two again against the pool size the engine
reports in its startup log (`GPU KV cache size: N tokens`). A scrape that finds no size
fails the run.

Alternatives considered:

- **The spec's workload of about 76 prefixes × 3000 tokens (≈230k) on the uncapped pool.**
  The tier would have to hold more than 230k tokens, which the g5.xlarge cannot afford.
- **Waiting for the g5.2xlarge.** That would block the experiment on the quota request.
  Capping the pool measures the same mechanism now, at a smaller scale.
- **Shrinking the pool through `--gpu-memory-utilization`.** That flag sets a fraction of
  GPU memory, and the pool is whatever is left after profiling. `--kv-cache-memory-bytes`
  sets the pool's size directly, so the guards can check it before deploy.

### 3 runs per arm, interleaved

The A/B runs each arm 3 times, in the order off-r1, on-r1, off-r2, on-r2, off-r3, on-r3.
Each run redeploys the pod with its arm's switch, runs one unrecorded warmup cell of the
measured workload at no more than the lowest rung's concurrency, then runs the full ladder.
Latency drifts from one redeploy to the next (#311), so interleaving spreads that drift
across both arms instead of letting it skew one. The warmup starts every run from a filled
cache. Each rung is reported as the median and range across an arm's runs. `analyze-kv-ab`
claims a difference on a metric only where the two arms' ranges do not overlap; with 3 runs
per arm, complete separation in the predicted direction has a one-sided permutation p of
1/20. Resume works on whole runs, because a redeploy clears both tiers.

Alternatives considered:

- **All off runs, then all on runs.** Any drift over the session would land on one arm.
- **One run per arm.** There would be no ranges, so noise could not be told apart from a
  win.
- **More than 3 runs per arm.** It costs more wall clock per A/B. 3 is a judgment call, to
  revisit if the spreads come out too wide to decide.

## Consequences

- With offload on, the replica takes 4 GiB more host memory: the pod limit is 12Gi, not
  8Gi. Any reported win is weighed against that cost.
- The A/B measures the mechanism on a capped 16,384-token pool, not the gain on the
  uncapped rig. On the committed g5.xlarge, offload adds no reuse, because the tier is
  smaller than the uncapped pool. A win there needs a tier larger than the pool, such as
  the g5.2xlarge.
- The A/B leaves the knob-sweep grid, its slugs, cache key and analyze-bench's parsing
  alone. The GPU KV pool cap is an engine arg in every cell's digest, uncapped for the
  knob sweep.
- L4 inherits a known-good setup: the stock image, `LMCacheConnectorV1` in `kv_both` role,
  and the `LMCACHE_*` tier env.
- Startup with offload on logs three harmless lines. vLLM turns off its hybrid KV cache
  manager (Qwen3-8B is full attention only). LMCache logs `ERROR: vLLM is not available but
  tried to query kv cache layout`. There is a `PYTHONHASHSEED` warning.
- The pod's OOM boundary moves with the memory limit, so the OOM cohort (#321) is worth
  watching on offload-on runs.
