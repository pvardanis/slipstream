# Operations

Runbooks for the live rig. All but cloud verify assume the stack is up
(`just stack-up`) and end with `just stack-down`.

## Knob sweep

The two-tier sweep ([ADR-0009](adr/0009-knob-sweep-rig-and-ceiling-method.md)) runs
unattended on the in-cluster Prefect worker. Every swept value lives in
[`bench/sweep-grid.yaml`](../bench/sweep-grid.yaml).

**From CI** — Actions → `sweep-dispatch` → Run workflow, **on `main`** (the role trusts
only `main`). Both inputs are optional: `instance_id` defaults to the running bench host
and `run_id` to a UTC timestamp. The workflow does not stand the stack up, and fails
before creating the run if no worker is polling the pool.

Each dispatch re-registers the deployment with defaults resolved from the live stack
and `main`'s image, so it **overwrites** any override a manual `just prefect-register`
set (a pinned `bench_image_tag`, retries).

**From a workstation:**

```sh
just prefect-register   # register the deployment on the worker's pool
just prefect-ui         # hold a port-forward open; UI at http://localhost:4200
# in another shell:
PREFECT_API_URL=http://127.0.0.1:4200/api uv run prefect deployment run knob-sweep/knob-sweep \
  --param run_id=<run-id> \
  --param instance_id="$(terraform -chdir=terraform/bench-endpoint output -raw bench_host_instance_id)"
```

A failed redeploy, ceiling scrape, or cell fails the run. **To resume**, run again with
the same `run_id` (a Retry in the Prefect UI reuses it): cells with a cache record under
that run are reused, and a point whose every cell is cached skips its redeploy. A cell whose
config changed since (model, vLLM or bench-client image, the point's knobs, the cell's
knobs, the load-cell flags) is re-measured. **A new `run_id` re-measures every cell**,
which is how to take a fresh sample; nothing in the bucket is deleted by hand
([ADR-0022](adr/0022-run-id-scoped-resume-on-prefects-cache.md)). A commercial and a
default run share cache entries under one `run_id`, so give them different ids.

The run aborts before its first redeploy if it cannot write and read back the results
bucket's `prefect/cache-keys` prefix; the error names the bucket.

**Results** land in the results bucket: each cell's JSON under
`sweeps/<run-id>/<point-slug>/` and the plots under `sweeps/<run-id>/charts/`. The
resume state (Prefect task results and cache records) lives beside them under
`prefect/results/` and `prefect/cache-keys/`. `stack-down` deletes the whole bucket.
Pull the results first, then build the artifacts locally:

```sh
just bench-results-sync
uv run slipstream-bench aggregate-sweep --run-dir bench/results/<run-id>   # ceiling table
uv run slipstream-bench chart --run-dir bench/results/<run-id>             # tables + plots
```

## One point, prefix cache

Against whatever engine point is currently deployed, over the bench endpoint:

```sh
just bench mns16_kvfp8_pcon   # the deployed engine point's client-load ladder (its grid slug)
just prefix-cache 90 1.0      # cold/warm prefix-cache hit-rate for one cell
```

`just bench` mints and prints a fresh run id, so each call re-measures the ladder. To
resume a failed one, pass that id back: `BENCH_RUN_ID=<run-id> just bench <point-slug>`.

## Benchmarking an unmerged branch

Pull requests never publish images, and every recipe defaults to the `-main` tag. Push
the branch's content image, then pin the run to it with the `bench_image_tag` variable:

```sh
just bench-image
just bench_image_tag="$(bench/image-tag.sh sha-tag)" bench <point-slug>
just bench_image_tag="$(bench/image-tag.sh sha-tag)" prefect-register   # for a knob sweep
```

A `sweep-dispatch` run re-registers with the `-main` image, so trigger a pinned knob
sweep from a workstation.

## Cloud verify

`just cloud-verify` is the hand-triggered, real-spend end-to-end check
([ADR-0002](adr/0002-infrastructure-testing-strategy.md)). Run it **from a clean slate** — it
aborts if the eks stack holds any resource, so `just stack-down` first. It needs the
orchestration image published, because `cluster-up` deploys the Prefect worker from it.

It runs `cluster-up` → `gpu-pool-up` → `gpu-deploy`, asserts the replica returns a
well-formed completion, tears everything down, then sweeps EC2 for any
`Project=slipstream` instance or volume still billing (the Karpenter `g5` and its root
volume live outside Terraform state). Teardown and the sweep run even if the smoke
fails. A green exit means the rig served and spend is back at zero.
