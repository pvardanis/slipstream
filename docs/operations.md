# Operations

Runbooks for the live rig. All but cloud verify and the spend-leak alert assume the stack
is up (`just stack-up`) and end with `just stack-down`.

## Knob sweep

The two-tier sweep ([ADR-0009](adr/0009-knob-sweep-rig-and-ceiling-method.md)) runs
unattended on the in-cluster Prefect worker. Every swept value lives in
[`bench/configs/sweep-grid.yaml`](../bench/configs/sweep-grid.yaml).

**From CI** — Actions → `sweep-dispatch` → Run workflow, **on `main`** (the role trusts
only `main`). Both inputs are optional: `instance_id` defaults to the running bench host
and `run_id` to a UTC timestamp. The workflow does not stand the stack up, and fails
before creating the run if no worker is polling the pool.

Each dispatch re-registers the deployment with defaults resolved from the live stack
and `main`'s image, so it **overwrites** any override a manual `just prefect-register-sweep`
set (a pinned `bench_image_tag`, retries).

**From a workstation:**

```sh
just prefect-register-sweep   # register the deployment on the worker's pool
just prefect-ui               # hold a port-forward open; UI at http://localhost:4200
# in another shell:
PREFECT_API_URL=http://127.0.0.1:4200/api uv run prefect deployment run knob-sweep/knob-sweep \
  --param run_id=<run-id> \
  --param instance_id="$(terraform -chdir=terraform/bench-endpoint output -raw bench_host_instance_id)"
```

The flow redeploys `deploy/vllm-gpu` for each engine point, so **do not run
`just gpu-deploy` while a sweep is in flight**: it recreates the replica under the running
cell, and the point's later cells measure the committed config while being recorded under the
point's knobs; the flow does not notice. After a sweep the replica keeps the last point's
knobs; `just gpu-deploy` with `MAX_NUM_SEQS`, `KV_CACHE_DTYPE`, `PREFIX_CACHING_FLAG`,
`KV_CACHE_OFFLOAD` and `KV_CACHE_MEMORY_BYTES` unset restores the committed config
([ADR-0015](adr/0015-unattended-sweep-cloud-server-and-parent-flow.md)).

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
`knob-sweep/<run-id>/<point-slug>/` and the plots under `knob-sweep/<run-id>/charts/`. The
resume state (Prefect task results and cache records) lives beside them under
`prefect/results/` and `prefect/cache-keys/`. `stack-down` deletes the whole bucket.
Pull the results first, then build the artifacts locally:

```sh
just bench-results-sync
uv run slipstream-bench aggregate-sweep --run-dir bench/results/knob-sweep/<run-id>   # ceiling table
uv run slipstream-bench chart --run-dir bench/results/knob-sweep/<run-id>             # tables + plots
```

## KV cache A/B

The `kv-cache-extension-ab` flow (#326) measures KV cache offload off against on at one
engine point, on the same in-cluster worker as the knob sweep. Its arm runs are
interleaved — off, on, off, on — `runs_per_arm` times. Each arm run redeploys
`deploy/vllm-gpu` with the arm's offload switch and the workload's capped GPU KV pool,
runs one unrecorded warmup cell, then the concurrency ladder.

**The workload** is [`bench/configs/kv-cache-extension-ab.yaml`](../bench/configs/kv-cache-extension-ab.yaml),
the one config the orchestration image bakes and the deployment runs. Other workloads are
parked beside it: `kv-cache-extension-ab.agent.yaml` (the live one is a copy of it) and
`kv-cache-extension-ab.chat.yaml`. To run a parked one, either copy it over the live file
and publish the orchestration image (a push to `main` publishes it; for a branch, see the
`e2e-test-orchestration-branch` skill), or copy it into the worker pod and pass
`--param kv_ab_config=/tmp/<file>` at trigger. The pod's `/tmp` does not survive a worker
restart.

**From CI** — Actions → `kv-ab-dispatch` → Run workflow, **on `main`**. It takes the
same optional inputs as `sweep-dispatch` and runs the same steps: both call
`.github/workflows/flow-dispatch.yml`. Each dispatch re-registers the deployment with
defaults from the live stack and the `-main` bench image, so it overwrites any override a
manual `just prefect-register-kv-ab` set, and runs the workload baked into the running
worker image. That image is pinned at `cluster-up`, so it can lag `main`; the job summary
warns when its workload differs from `main`'s.

**From a workstation:**

```sh
just prefect-register-kv-ab   # register the deployment on the worker's pool
just prefect-ui               # hold a port-forward open; UI at http://localhost:4200
# in another shell:
PREFECT_API_URL=http://127.0.0.1:4200/api uv run prefect deployment run \
  kv-cache-extension-ab/kv-cache-extension-ab \
  --param run_id=<run-id> \
  --param instance_id="$(terraform -chdir=terraform/bench-endpoint output -raw bench_host_instance_id)"
```

As with the knob sweep, **do not run `just gpu-deploy` while an A/B is in flight**. After
an A/B the replica keeps the last arm's offload switch and capped pool; the knob sweep's
restore step above returns it to the committed config.

**Refusals.** Before the first redeploy, the run fails if a request (`total_len` plus
`output_len`) is past the manifest's max-model-len, if the prefix is not whole LMCache
chunks, if the prompts' chunks overflow the manifest's CPU tier, or if the configured GPU
KV pool holds the working set or cannot hold the top rung's requests. After each redeploy
the pool check runs again on the size the engine logged at startup. Every rung also checks
the KV connector's counters (`vllm:external_prefix_cache_*`) against its arm:

- an **off-arm** rung that advanced them ran on an engine that offloads KV cache;
- an **on-arm** rung that sent the connector no queries ran on an engine without the
  LMCache connector. Check the vLLM pod's `KV_CACHE_OFFLOAD` and its startup log for the
  connector.

Either refusal fails the run with no record written for that rung.

**To resume**, run again with the same `run_id`. Resume is per whole arm run: a redeploy
clears both cache tiers, so an arm run whose every rung is cached is skipped, and any
other is redeployed and re-measured whole. A new `run_id` re-measures everything.

**Results** land in the results bucket under `kv-offload-ab/<run-id>/`. Each rung's raw
result is under `<arm>-r<n>/<cell>.json`, and its labels (arm, run, and prefix-cache hit
rates) beside it as `<arm>-r<n>/<cell>.kv_ab.json`; the chart is under `charts/`. Each arm
run's warmup cell lands beside its rungs under `<arm>-r<n>/warmup/`, unlabelled; leave it
out when folding raw results by hand. `just bench-results-sync` pulls the run into
`bench/results/kv-offload-ab/`.

The chart is public, like the knob sweep's: the bucket serves any `charts/` path under
`kv-offload-ab/` anonymously. So never use `charts` as a run id or a run id segment, or
that run's raw results become public too.

## One point, prefix cache

Against whatever engine point is currently deployed, over the bench endpoint:

```sh
just bench mns64_kvfp8_pcon   # the deployed engine point's client-load ladder (its grid slug)
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
just bench_image_tag="$(bench/image-tag.sh sha-tag)" prefect-register-sweep   # for a knob sweep
just bench_image_tag="$(bench/image-tag.sh sha-tag)" prefect-register-kv-ab   # for a KV cache A/B
```

A `sweep-dispatch` or `kv-ab-dispatch` run re-registers with the `-main` image, so
trigger a pinned run from a workstation.

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

## Spend-leak alert

The spend-leak guard ([ADR-0025](adr/0025-spend-leak-guard-outside-the-cluster.md)) emails
the operator through the `slipstream-spend-leak` SNS topic when the account bills with no
session running. It lives in `terraform/bootstrap`, so it outlasts `just stack-down`. Two
alerts reach the inbox:

- **`slipstream-gpu-spend-leak`** (CloudWatch alarm): on-demand G-class vCPUs have run for
  20 hours. A GPU is almost certainly left up.
- **`slipstream-monthly`** (AWS Budget): month-to-date spend crossed 50%, 75% or 90% of
  $200, or the month's forecast crossed 100%. It covers all spend (NAT, ALBs, bench
  host, control plane, spot), and lands hours after the spend.

**On a GPU alarm**, tear the rig down and let the leak sweep report what survived:

```sh
just stack-down
```

If the GPU belongs to no stack (say, a hand-launched instance), find it and stop it:

```sh
aws ec2 describe-instances --region eu-west-1 \
  --filters Name=instance-state-name,Values=running Name=instance-type,Values='g*' \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,LaunchTime,Tags[?Key==`Name`]|[0].Value]' \
  --output table
```

The alarm returns to OK once no G-class vCPU runs; it emails only on the way into ALARM.

**On a budget alert**, find what is billing this month (Cost Explorer bills $0.01 per call):

```sh
aws ce get-cost-and-usage --time-period Start="$(date -u +%Y-%m-01)",End="$(date -u +%Y-%m-%d)" \
  --granularity MONTHLY --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE
```

**Setup.** `just bootstrap` needs the operator's address, kept out of this public repo:
`operator_email` in the gitignored `terraform/bootstrap/terraform.tfvars`, or
`TF_VAR_operator_email`. AWS then emails a confirmation link; nothing is delivered until
it is clicked, and every bootstrap plan warns (`operator_email_confirmed`) while it is
pending. An unsubscribe link in an alert email removes the subscription; `just bootstrap`
re-creates it.

**Rehearsal.** Force the alarm and check the email arrives:

```sh
aws cloudwatch set-alarm-state --region eu-west-1 --alarm-name slipstream-gpu-spend-leak \
  --state-value ALARM --state-reason "spend-leak rehearsal"
aws cloudwatch describe-alarm-history --region eu-west-1 --alarm-name slipstream-gpu-spend-leak \
  --max-records 3 --query 'AlarmHistoryItems[].[Timestamp,HistorySummary]' --output text
```

The history shows `Alarm updated from OK to ALARM` and then `Successfully executed action
arn:aws:sns:...:slipstream-spend-leak`; the alarm returns to OK at its next evaluation.

### Rehearsal log

| Date | Rehearsal | Result |
|---|---|---|
| 2026-10-26 | First apply of the guard (#362), then the rehearsal above | ALARM at 10:42:36 UTC, SNS action executed, email received. A plan of the budget, alarm and topic policy afterwards: no changes. |
