<!-- ADR recording the results bucket's layout: one root prefix per experiment (knob-sweep/, kv-offload-ab/) beside prefect/ and boot-status/, the root carried in the bench host's RUN_ID rather than a fixed sweeps/ prefix, each cell result stored once, and the KV cache A/B's rung labels written as a <cell>.kv_ab.json object beside the host's raw result so the worker never writes the host's objects. Amends the paths of ADR-0018 and ADR-0022 and the record of ADR-0017's 2026-10-20 amendment. -->

# ADR-0024: The results bucket is rooted per experiment and stores each result once

- Status: Accepted
- Date: 2026-10-22

## Context

The bench host's `bench-sweep.sh` copied every cell result under a fixed `sweeps/` prefix,
whatever experiment ran it. The knob sweep's cells landed under `sweeps/<run>/<point>/`.
The KV cache A/B (#326) nested its run id under `kv-ab/`, so its raw results landed under
`sweeps/kv-ab/<run>/<arm>-r<n>/`. The worker then wrote each rung's labelled record, the raw
result plus a `kv_ab` block, under a second root, `kv-ab/<run>/<arm>-r<n>/`. That stored
every A/B rung twice, and it spread one experiment over two roots, one of them inside the
other experiment's.

The worker's IAM role keeps its writes off the host's raw results: it may put objects only
under its own prefixes, never the host's.

## Decision

### One root prefix per experiment

The bucket root holds one prefix per experiment, beside the worker's Prefect state and the
bench host's boot status:

```
boot-status/
prefect/results/, prefect/cache-keys/
knob-sweep/<run>/<point-slug>/<cell>.json
knob-sweep/<run>/charts/<plot>.png
kv-offload-ab/<run>/<arm>-r<n>/<cell>.json
kv-offload-ab/<run>/<arm>-r<n>/<cell>.kv_ab.json
kv-offload-ab/<run>/<arm>-r<n>/warmup/<cell>.json
kv-offload-ab/<run>/charts/<plot>.png
```

Nothing from one experiment lands under another's root. `prefix-cache/`, which the
standalone prefix-cache measurement writes, is a root of the same kind.

### The root rides in the host's `RUN_ID`

`bench-sweep.sh` copies a cell to `s3://<bucket>/<RUN_ID>/`. The orchestration layer, which
knows the experiment, roots the `RUN_ID` it sends. The A/B's run id is already rooted:
`kv-offload-ab/<run>/<arm>-r<n>`, which is also its cells' cache-key run id. The knob
sweep's run id stays `<run>/<point-slug>`, because its point flows tag their Prefect runs
with the run id's leading segment and the parent tags `run=<run>`; a rooted run id would
split that group. Its S3 addresses root the run id through one function,
`get_knob_sweep_prefix`.

### Each result is stored once

The host's raw result is the rung's only copy. The worker writes just the rung's labels
(arm, run, the prefix-cache counters' advance, the two hit rates) beside it as
`<cell>.kv_ab.json`. The cell task returns the labels' pointer. The terminal render reads
each rung's labels, reads the raw result beside them, and joins the two into the record the
report folds (`build_kv_ab_record`), so the record exists only in memory and the report
member is unchanged.

The worker's PutObject is scoped to `kv-offload-ab/*.kv_ab.json`. IAM's `*` spans `/`, so
the suffix, not a path segment, is what keeps the grant off the host's `<cell>.json` beside
it.

### The public charts prefixes follow the roots

The anonymous read ADR-0018 grants covers `knob-sweep/*/charts/*` and
`kv-offload-ab/*/charts/*`, and the worker's chart PutObject the same two patterns. The
condition ADR-0018 states still holds per root: no run id may have a `charts` segment.

## Alternatives considered

- **The worker overwrites the host's raw result with the labelled record.** One object per
  rung and no join at read time, but the worker would need PutObject on the host's results,
  and a rung whose labelling failed would sit unlabelled under the same key.
- **A fixed prefix per experiment in `bench-sweep.sh`.** The host would need to know which
  experiment it runs; the orchestration layer already composes the run id, so the root goes
  there.
- **Rooting the knob sweep's run id.** One rule for both experiments, but it changes the
  knob sweep's Prefect tags, run grouping and cache keys for a storage concern.

## Consequences

- The bench host's user data carries `bench-sweep.sh`, so a host provisioned before this
  change still writes under `sweeps/`. The host and the worker image move together:
  re-provision the bench endpoint and re-pin the worker.
- A run id started under the `sweeps/` layout cannot resume across this change: its cached
  cells' objects sit under `sweeps/`, where the render no longer reads them. Start a new run
  id.
- Reading an A/B rung back takes two GETs instead of one. Both objects sit beside each other,
  and the render already reads one rung at a time.
- The local sync mirrors the bucket: `bench/results/knob-sweep/` and
  `bench/results/kv-offload-ab/`.
- The S3 prefix is `kv-offload-ab`, while the flow is `kv-cache-extension-ab` and the
  glossary's short name is `kv-ab`.
