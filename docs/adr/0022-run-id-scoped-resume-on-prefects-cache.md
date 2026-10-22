<!-- ADR recording that Prefect's task cache is the single source of truth for whether a sweep cell is complete, reversing ADR-0012's "the JSON wins on disagreement": a cell is identified by run id + a config digest narrowed to the inputs that change its numbers + engine point + cell; the knob sweep's redeploy-skip gate looks up the same cache records with the same key function instead of reading cell JSON; a flow-start preflight write/read-probes the cache-key storage so a bad bucket aborts before a GPU redeploy; re-measuring means a new run id, never hand-deleting S3 objects; and the commercial-arm collision is a known gap. Supersedes in part ADR-0012's resume/caching sections and ADR-0015's redeploy-skip gate paragraph. The 2026-10-22 amendment moves the cell objects under knob-sweep/ (ADR-0024). -->

# ADR-0022: Resume is scoped to a run id and decided by Prefect's cache

- Status: Accepted
- Date: 2026-10-15
- Supersedes: in part, the resume and caching sections of
  [ADR-0012](0012-sweep-resumability-and-orchestrator-choice.md) ("Resume is per-cell, keyed on a
  deep config digest", the `Skip` key, "The per-cell JSON in S3 is the single source of truth") and
  the redeploy-skip gate paragraph of
  [ADR-0015](0015-unattended-sweep-cloud-server-and-parent-flow.md) ("A resume skips a point's
  redeploy when its cells are already done")

## Context

A knob sweep decided "is this cell already measured?" in two places, keyed differently.
The cell task's Prefect cache (ADR-0012) was keyed `digest:point-slug:cell-name`, with a
digest over `model.yaml`, the whole `sweep-grid.yaml`, and the vLLM image ref: the run id
was not in the key. The redeploy-skip gate (ADR-0015) checked whether a valid cell JSON
existed under `sweeps/<run-id>/<point-slug>/`: the run id was in the key and the digest
was not (#241).

When the two disagreed, the sweep broke or measured the wrong thing:

- **Wrong numbers, no error.** Resuming the same run id after a model or vLLM image change,
  the gate found the old JSON and skipped the redeploy, while the cache missed on the new
  digest and re-ran every cell against whichever engine was deployed last.
- **A broken re-dispatch.** CI mints a fresh run id by default. The gate saw no cells and
  redeployed; the cache then hit on every cell and returned the previous run's pointers, so
  nothing was written under the new run and render failed on a 404.
- **A redeploy loop** when the validity threshold was tightened: the gate re-validated and
  reported the point pending, the cache still hit, and every resume redeployed again.
- **Over-invalidation.** Any byte change to the grid file — one added point, one comment —
  moved the digest of every cell of every point.
- **Divergent entry points.** `just bench` used only the cache; the knob sweep used both.

Two mechanisms meant the operator could not predict which one decided a given cell.

The knowledge base has no Prefect doc. `distributed-ml-patterns.md` §4 ("Step memoization")
frames memoization as content-hashing a step's inputs and warns that "missing a dimension →
false cache hits", which is the first failure above. `data-systems.md` Part III treats a
cache as a derived view of one authoritative source ("Pick one source of truth"), not a
second copy kept in step by hand. The Prefect behaviour below — a cache
hit is a COMPLETED state named "Cached", and the result store's existence check returns
False on any read error — was confirmed against Prefect 3.8.6.

## Decision

### Prefect's cache record decides whether a cell is complete

A cell is complete when its cache record exists under the results bucket's cache-key prefix
(`prefect/cache-keys`). This reverses ADR-0012's "if Prefect's cache ever disagrees with the
JSON, the JSON wins": the cache record is authoritative. The per-cell JSON the bench host
writes under `sweeps/<run-id>/` stays the measurement data that render and
`just bench-results-sync` read; it no longer decides completion.

The validity gate is unchanged in role. The cell task validates the JSON it just produced
and raises on an invalid or degenerate result (ADR-0012), so a failure never gets a cache
record and the next run retries it.

### A cell's identity is run id + config digest + engine point + cell

The cache key is `run-id:digest:point-slug:cell-name`, built by one function
(`orchestration/cache_key.py`). Every part must be non-blank and free of the `:` separator.
The run id arrives as a cell-task call parameter, like the other three. On both entry
points — the knob sweep and `just bench` — the run-id part is the point's bucket prefix,
`<run-id>/<point-slug>`, the prefix its cell JSON lies under.

- A **new run id** always measures every cell fresh, so each run's prefix holds a complete
  grid of its own results.
- **Resuming** means re-running with the same run id. Within that run a cell is reused only
  while its digest is unchanged.

The single-point path and the knob sweep share the key function and the cell task, so they
resume by the same rule.

### The digest covers exactly the inputs that change a cell's numbers

The digest is a sha256 over length-framed inputs:

- the raw bytes of `model.yaml`;
- this engine point's engine args and the cell's knobs, as canonical (sorted-key) JSON
  rather than the raw grid-file bytes;
- the vLLM serving image ref, read from the `vllm` container in `k8s/vllm-gpu.yaml`;
- the bench-client image ref;
- the extra load-cell flags (`--dry-run` and any host-flag override), minus the
  credential-only `--api-key-env` and its value;
- the validity threshold (`DEFAULT_MAX_ERROR_RATE`).

Adding another engine point or editing a grid comment changes no other cell's digest, and
changing one point's knobs invalidates only that point's cells. A dry-run cell and a real
cell never share an entry. Tightening the threshold re-measures cells accepted under the
looser one. `model.yaml` stays hashed as raw bytes: a whitespace edit then re-measures
rather than reusing, which fails safe.

Orchestration source code stays out of the digest, as ADR-0012 scoped it.

### The redeploy-skip gate reads the same cache records

The knob sweep still skips a point's GPU redeploy and ceiling scrape when the point has no
pending cells, through the same injected `has_pending_cells` predicate (ADR-0015). The
production predicate no longer reads cell JSON. For each of the point's cells it builds the
key with the cell task's key function, from the same point context the point sweep uses, and
asks the result store Prefect derives from the cell task — its result storage and its cache
policy's key storage — whether a record exists. A point is pending if any cell lacks one. A
task set to refresh its cache reports every cell pending.

So the gate and the task cannot disagree: a point is skipped only when every one of its
cells will be served from cache. A failed cell fails the run and leaves its point pending,
so the next resume redeploys that point and re-runs every cell without a cache record: the
failed cell, the point's cells after it, and every later point. The per-point "run" vs
"resumed" summary follows the gate's decision.

### A preflight probes the cache-key storage before any redeploy

Prefect's existence check reads any storage error as "no record". A misconfigured bucket or
a denied grant would therefore make every cell look pending and trigger a ~20-minute
redeploy for nothing. Right after the cell task registers its storage blocks, before any
cell runs, it writes a fixed probe object to the cache-key storage and reads it back. Any
failure aborts the run with an error naming `s3://<bucket>/prefect/cache-keys`, before the
first redeploy. Both entry points build the cell task the same way, so both run the preflight.

### Re-measuring means a new run id, never hand-deleting objects

To re-measure, start a new run id, or resume after a digest input changes. Nothing is
deleted from S3 by hand, so cache records and cell JSON cannot drift apart. This replaces
ADR-0012's "delete-and-resweep" for re-sampling a cell.

## Consequences

- Shipping the key and digest change makes every existing cache record a miss once. No
  migration: the results bucket is destroyed with the stack.
- Resume is per run id. A sweep-dispatch without a `run_id` input mints a timestamp and
  re-measures the whole grid; resuming means dispatching again with the failed run's id.
- The result and cache-key storage blocks, their prefixes, the worker's IAM grants, and the
  Terraform test asserting those prefixes are unchanged.
- Cache records are never expired or garbage-collected. They live and die with the
  results bucket.
- **Known gap: the commercial arm collides with the default arm.** The `commercial` flag is
  in neither the cell basename nor the cache key, so a commercial and a non-commercial run of
  the same cell, under the same run id, share one S3 object and one cache entry. Run the two
  arms under different run ids until this is fixed.
- Folding a new meaning-bearing input into a cell means adding it to the digest; one missed
  is a silent stale reuse, the failure this ADR removes.

## Amendment (2026-10-22): the cells live under `knob-sweep/`

The results bucket is rooted per experiment (ADR-0024): a knob sweep's cell objects live
under `knob-sweep/<run-id>/<point-slug>/`, not `sweeps/<run-id>/<point-slug>/`. The cache
key's run id stays `<run-id>/<point-slug>`, so the identity this ADR defines is unchanged.
