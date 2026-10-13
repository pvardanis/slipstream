# Operations

Runbooks for the live rig. All but cloud verify assume the stack is up
(`just enable_prefect_worker=true stack-up`) and end with `just stack-down`.

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

A point that fails to deploy, or whose ladder reports failures, is recorded and the
sweep continues, exiting non-zero at the end. A rerun skips any cell whose config
already holds a valid measurement.

**Results** land in the results bucket under `sweeps/<run-id>/`, which `stack-down`
deletes. Pull them first, then build the artifacts locally:

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
aborts if the eks stack holds any resource, so `just stack-down` first.

It runs `cluster-up` → `gpu-pool-up` → `gpu-deploy`, asserts the replica returns a
well-formed completion, tears everything down, then sweeps EC2 for any
`Project=slipstream` instance or volume still billing (the Karpenter `g5` and its root
volume live outside Terraform state). Teardown and the sweep run even if the smoke
fails. A green exit means the rig served and spend is back at zero.
