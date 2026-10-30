# Operations

Runbooks for the live rig. All but cloud verify, the Grafana Cloud setup, the Grafana data
sources and dashboards, and the spend-leak alert assume the stack is up (`just stack-up`) and end with
`just stack-down`.

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

## Grafana Cloud setup

Grafana Cloud's free tier holds the dashboards, metrics and traces
([ADR-0026](adr/0026-fleet-observability-on-grafana-cloud-free.md)). The bootstrap stack
creates three empty Secrets Manager secrets, `grafana-cloud/write`, `grafana-cloud/api` and
`grafana-cloud/stack-read`, and the IAM role the stack's CloudWatch data source assumes
(Grafana Assume Role). The tokens are minted by hand in Grafana Cloud, so a wizard walks
through it once per environment:

```sh
just grafana-cloud-setup
```

It checks `terraform` and `aws` are installed, `aws` is logged in, and AWS CLI history is
off (history would record each token), then runs eight stages:

1. Create a free Grafana Cloud stack and paste its URL.
2. Copy the External ID from the CloudWatch data source's Connection Details; it is
   written as `grafana_external_id` into `terraform/bootstrap/terraform.tfvars`. Then
   delete that data source: picking the CloudWatch type creates one named `CloudWatch`,
   and `just grafana-up` creates its own under the same name.
3. Enter the spend-leak alert address as `operator_email`. Skipped when the tfvars file or
   `TF_VAR_operator_email` already sets it.
4. Apply the bootstrap stack.
5. Mint the access-policy token for metrics and traces writes, stored in
   `grafana-cloud/write`.
6. Mint the service account token for the Grafana Terraform provider, stored in
   `grafana-cloud/api`. The service account needs the Admin role, as only Admin can
   create data sources. If `slipstream-terraform` already exists, change its role to
   Admin and reuse it.
7. Mint the Cloud access-policy token the Grafana provider reads the stack's public send
   endpoints with, stored in `grafana-cloud/stack-read`. The policy is made in the Cloud
   Portal (Security → Access Policies), not in the stack: name it `slipstream-stack-read`,
   realm this stack, scope `stacks:read` only.
8. Check all three secrets hold a value, then write the stack URL as `grafana_url` into
   `terraform/grafana/terraform.tfvars`.

The wizard pipes tokens to `aws secretsmanager put-secret-value` on stdin, so they never
reach a file or the command line. A failed stage stops the wizard; rerun it from the
start.

On a setup made before the stack-read token existed, `just bootstrap` creates
`grafana-cloud/stack-read` empty and `just grafana-up` fails reading it. Mint the token as
in stage 7, then store it without rerunning the wizard:

```sh
read -rs t && printf '%s' "$t" | aws secretsmanager put-secret-value --region eu-west-1 \
  --secret-id grafana-cloud/stack-read --secret-string file:///dev/stdin; unset t
```

`grafana_external_id` and `operator_email` have no default. A later `just bootstrap` takes
each from the gitignored tfvars file or its `TF_VAR_`; when neither sets it, as in a fresh
worktree with no tfvars file, it reads the value back from AWS: the external ID from the
`slipstream-grafana-cloudwatch` role's trust policy, the email from the
`slipstream-spend-leak` topic's email subscription. Terraform prompts for a value only when
there is nothing to read: no bootstrap state yet, a state that lacks the role or topic
output (that apply adds it, so this happens once), or a topic with no email subscription.
A failed AWS read, or one that returns more than one external ID or address, stops before
the apply, naming the variable to set.

## Grafana Cloud write token in the cluster

`just cluster-up` installs the External Secrets Operator, which copies the
`grafana-cloud/write` token from Secrets Manager into the `monitoring` and `slipstream`
namespaces as the Secret `grafana-cloud-write`, key `token`
([ADR-0026](adr/0026-fleet-observability-on-grafana-cloud-free.md)). The apply does not
wait for that copy, so check it after `cluster-up`:

```sh
kubectl get externalsecret -n monitoring
kubectl get externalsecret -n slipstream
```

Both should show `grafana-cloud-write` with status `SecretSynced` and ready `True`. To
confirm the token is non-empty without printing it:

```sh
kubectl get secret grafana-cloud-write -n monitoring -o jsonpath='{.data.token}' | wc -c
kubectl get secret grafana-cloud-write -n slipstream -o jsonpath='{.data.token}' | wc -c
```

A count of 0, or a missing Secret, means the sync into that namespace failed; each
namespace syncs on its own.
`kubectl describe externalsecret grafana-cloud-write -n <namespace>` shows why in its
status and events. A `grafana-cloud/write` secret that `just grafana-cloud-setup` never
filled has no version, so the read fails, the `ExternalSecret` shows `SecretSyncedError`,
and no Secret is created.

The operator re-reads the secret hourly. After replacing the token, force an immediate
sync in each namespace:

```sh
kubectl annotate externalsecret grafana-cloud-write -n monitoring force-sync="$(date +%s)" --overwrite
kubectl annotate externalsecret grafana-cloud-write -n slipstream force-sync="$(date +%s)" --overwrite
```

## Grafana data sources

The `terraform/grafana` stack owns the data sources the dashboards query
([ADR-0026](adr/0026-fleet-observability-on-grafana-cloud-free.md)). Once
`just grafana-cloud-setup` is done:

```sh
just grafana-up
```

It initialises the stack against the state bucket, then plans and asks before applying;
the stack persists across sessions, so read the plan first. It has no down recipe:
`just cluster-down` and `just stack-down` leave it in place.

- **CloudWatch** is created by the stack. It authenticates with Grafana Assume Role
  through the bootstrap stack's IAM role, so no AWS key is stored in Grafana.
- **Prometheus and Tempo** come with every Grafana Cloud stack as
  `grafanacloud-<stack>-prom` and `grafanacloud-<stack>-traces`. The stack reads them by
  name rather than managing them.
- **Send endpoints:** where Prometheus `remote_write` and the Collector send is read from
  the Grafana Cloud API, not from those data sources, whose URLs name Grafana-internal
  hosts no sender can reach. The plan fails if the API reports no public
  `https://<host>.grafana.net` endpoint or no instance ID. `just cluster-up` reads the
  outputs and publishes the endpoints and usernames the Collector reads as the ConfigMap
  `grafana-cloud-endpoints` in `slipstream`, so run `just grafana-up` before it.

The Grafana provider signs in with the `grafana-cloud/api` token and reads the Cloud API
with the `grafana-cloud/stack-read` token, both read from Secrets Manager. Both tokens are
therefore held in the stack's state, in the encrypted state bucket. The stack URL comes
from `grafana_url` in the gitignored `terraform/grafana/terraform.tfvars`, or from
`TF_VAR_grafana_url`; with neither set it defaults to `https://slipstreamobs.grafana.net`.

After the apply, open the CloudWatch data source and click Save & test. The role reads
CloudWatch metrics only, so the test reports the metrics query as working and the logs
query as failing; that is expected.

## Grafana dashboards

`just grafana-up` also applies every dashboard in `terraform/grafana/dashboards/`, one per
JSON file, keyed by file name: adding, editing or deleting a file creates, updates or
deletes that dashboard on the next apply. The dashboards are read-only in the Grafana UI,
and the next apply overwrites any change made there. Panels name their data source as
`${DS_PROMETHEUS}` or `${DS_TEMPO}`, which the stack replaces with the hosted Prometheus or
Tempo data source's UID.

- **Fleet health** (`fleet-health.json`,
  [`/d/fleet-health`](https://slipstreamobs.grafana.net/d/fleet-health)): `vllm-gpu`
  replicas available against desired, and GPU busy time and memory-bandwidth active,
  each as a fleet average and per pod. Neither GPU panel tracks load: busy time is near
  100% whenever a request is in flight, and memory-bandwidth active falls as batching
  shares the weight reads. Read load from the serving and throughput panels:
  - **Serving:** KV-cache usage, requests waiting, TTFT p95 and e2e latency p95, fleet-wide
    and per pod. An exemplar on the fleet e2e latency line opens the request's trace.
    From 15 s to 120 s the e2e p95 can be off by up to a factor of 1.5, as it is
    interpolated inside a latency bucket.
  - **Throughput and goodput:** output tokens/s, prompt tokens/s, requests/s, and live
    goodput, the share of requests meeting the bench configs' goodput gates (TTFT ≤ 1 s,
    TPOT ≤ 50 ms). Live goodput reads as the bench report's `goodput_fraction`; compare it
    per bench cell, as the panel's value at one instant covers a sliding window.
  - **Requests over the TTFT gate:** a Tempo table of `llm_request` spans with
    `ttft_ms` over 1 s, by `request_id`. It lists the first 100 matches in the window,
    sorted slowest first, not the 100 slowest. If it is empty while TTFT p95 is over 1 s,
    check that traces reach Tempo, that their spans carry `ttft_ms`, and that the query's
    literal is a decimal.

  The span-metric panels (e2e latency, live goodput) update once a minute, when the
  Collector flushes its span metrics, and read series the Collector sends to Grafana Cloud
  itself, listed under `collector_metrics` in the allowlist.

A panel shows "no data" when it queries a metric neither Prometheus nor the Collector
sends to Grafana Cloud. Check a dashboard's queries against `terraform/eks/remote-write-allowlist.yaml`:

```sh
just dashboard-allowlist-test
```

It fails on a dashboard that does not parse, on a metric missing from the allowlist, and on
a query whose metrics it cannot read: a `__name__` matcher, a Grafana variable as the
metric name, or a query variable. CI runs it in the shell-tests job.

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
`TF_VAR_operator_email`; `just grafana-cloud-setup` asks for it when neither is set. Once
the topic has its email subscription, confirmed or pending, `just bootstrap` with neither
set reads the address back from it. AWS emails a confirmation link; nothing is delivered until it is clicked,
and every bootstrap plan warns (`operator_email_confirmed`) while it is pending. An
unsubscribe link in an alert email removes the subscription; `just bootstrap` re-creates
it, prompting for the address when neither the tfvars file nor `TF_VAR_operator_email`
sets it.

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
