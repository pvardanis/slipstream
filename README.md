# slipstream

A self-hosted LLM inference platform on AWS EKS: open models served on spot GPUs,
with the cluster-level machinery around the model as the focus — autoscaling on
inference signals, spot-eviction survival mid-stream, cold-start elimination on
large weight loads, inference-aware routing, and prefill/decode disaggregation.
`just cluster-up` / `just cluster-down` conjures and destroys GPU capacity. It runs
cost-competitive against commercial APIs and proves it on a live scoreboard.

The name carries the platform's job: a slipstream is the low-drag wake behind a
fast-moving object. Every layer here reduces drag — prefix caching, KV-cache
offload, cold-start removal, prefill/decode split — so more tokens travel per
dollar. Token streaming and speculative decoding (a draft model riding the big
model's slipstream) sit literally inside the word.

The build spec is [`docs/spec.md`](docs/spec.md); the decision log at its end
traces every choice back to its ticket.

## Provisioning

All infrastructure is Terraform; the AWS console is never touched. A `justfile`
drives it. Credentials come from your `AWS_PROFILE` — Terraform never handles them.
Run `just` (or `just --list`) to see the recipes. Each lives in a file under
`just/` named for its lifecycle concern, and `just --list` sections them under that
concern's heading:

| Concern | Key recipes | Covers |
| --- | --- | --- |
| `cluster` | `bootstrap`, `cluster-up` / `cluster-down`, `gpu-pool-up` | Remote-state bootstrap, EKS create/plan/destroy, the GPU node pool. |
| `serve` | `gpu-deploy`, `gpu-up` / `gpu-down` | The vLLM api-key Secret, the GPU replica, its scale and completion smokes. |
| `bench` | `bench-image`, `bench`, `knob-sweep`, `prefix-cache` | The bench-client image, the load and knob sweeps, prefix-cache measurement, results sync, the ephemeral mTLS bench endpoint. |
| `obs` | `obs-up` / `obs-down`, `obs-pivot` | The OTel Collector spine stub — deploy, teardown, trace pivot. |
| `test` | `cli-test` | Local smokes: the Python suite and the shell tests for the OTel spine and bench image (no cluster). |
| `orchestrate` | `stack-up` / `stack-down`, `cloud-verify` | Whole-stack up/down, the one-shot cloud verification, the zero-leak spend sweep. |

The sections are display-only: `just` imports the six files into one flat
namespace, so any recipe calls any other unqualified (`just stack-up` chains
`cluster-up gpu-pool-up gpu-deploy bench-endpoint-up`) regardless of concern. The
`[group()]` attribute only tells `--list` where to file each recipe. The split and
the flat-namespace choice are recorded in
[`docs/adr/0013`](docs/adr/0013-justfile-imported-lifecycle-modules.md).

Prerequisites: `terraform` (>= 1.11), `just`, `kubectl`, and the AWS CLI, with an
`AWS_PROFILE` that can create VPC/EKS resources (see [AWS access](#aws-access)).
The benchmark recipes also need `docker` (to build the bench-client image); the
bench host is driven over SSM, so no SSH client is required. Optional inspection
tools: `k9s`, `stern`, `kubens`.

```sh
just bootstrap       # one-time: create the S3 remote-state bucket
just cluster-up      # create the cluster, then `kubectl get nodes`
just gpu-pool-up     # apply the GPU node pool + NVIDIA device plugin
just gpu-deploy      # roll out the GPU vLLM replica (Karpenter brings up the g5)
just gpu-completion  # port-forward the service and curl a completion out of it
just cluster-down    # destroy the cluster; spend returns to zero
```

`just bootstrap` runs once; its own state lives in the S3 bucket it creates (the
bucket name is a literal in its backend, since bootstrap is the root of the state
chain — see ADR-0005). After that, `just cluster-up` / `just cluster-down` are the
cluster lifecycle. The task-runner and
state-bootstrap choices are recorded in
[`docs/adr/0001`](docs/adr/0001-task-runner-and-state-bootstrap.md).

`just gpu-pool-up` applies the GPU node pool (NodePool + EC2NodeClass) and the
NVIDIA device plugin; `just gpu-deploy` applies [`k8s/vllm-gpu.yaml`](k8s/vllm-gpu.yaml):
a single vLLM replica serving the AWQ-quantized model from
[`model.yaml`](model.yaml) over the OpenAI API. Karpenter provisions a `g5.xlarge`
on demand once the pod requests a GPU, so `gpu-deploy` covers node bring-up, the
~10 GB image pull, and the weight load. The service is `ClusterIP` only — no public
endpoint, no cloud load balancer — so `just gpu-completion` reaches it through
`kubectl port-forward`. `just gpu-down` scales the replica to zero and Karpenter
reaps the `g5` (ADR-0006); `just cluster-down` tears the cluster down with nothing
left behind.

### AWS access

Work from a **dedicated IAM user, not the account root**. Root can't be granted
an EKS access entry, so the console's EKS **Resources** tab shows `Unauthorized`
for root and the cluster only trusts whoever ran `just cluster-up`.

1. As root (one time), create an IAM user (e.g. `slipstream-admin`) with the
   permissions to manage VPC/EKS, and enable **console access** + MFA for it.
2. Give it an **access key**, then configure a local profile:
   ```sh
   aws configure --profile slipstream-admin   # paste the access key + region (eu-west-1)
   export AWS_PROFILE=slipstream-admin         # the profile just/terraform/kubectl use
   ```
3. Sign in to the AWS console **as that user** (not root) at
   `https://<account-id>.signin.aws.amazon.com/console` to inspect EKS there.

`just cluster-up` grants cluster-admin to this caller automatically
(`enable_cluster_creator_admin_permissions`), so `kubectl` works immediately.

### Inspecting the cluster

The workload lives in the `slipstream` namespace (created by `just gpu-deploy`).

```sh
kubens slipstream                     # set the default namespace (no more -n flags)
k9s                                   # live TUI: pods, logs (l), describe (d), events
stern vllm -n slipstream --tail 50    # tail vLLM logs, follows pod restarts
```

`k9s` is the fastest way to see pod status and events; `stern` beats
`kubectl logs` during a crash loop because it re-attaches to each new pod.

## Benchmark harness

The L0 benchmark harness ships as `slipstream-bench`, a Python package managed by
[`uv`](https://docs.astral.sh/uv/). Its `typer` CLI dispatches the benchmark
subcommands below (plus `zero-leak`, the teardown sweep `just cloud-verify`
classifies with). The rewrite from bash is recorded in
[`docs/adr/0003`](docs/adr/0003-l0-bench-harness-in-python.md).

- **`load-sweep`** — fire `vllm bench serve` across a prefix-share × burstiness
  grid, writing one result JSON per cell. The grid axes (`prefix-share` default
  `10 50 90`, `burstiness` default `0.2 1.0`, where `1.0` is Poisson and lower is
  burstier), the per-cell lengths and prompt counts, and the `seed` that fixes
  prompt synthesis so runs replay identically all come from the experiment YAML
  passed as `--config` (default `bench/load-sweep.yaml`); `--base-url`, `--model`,
  `--out-dir` and `--api-key-env` set the execution context, and `--dry-run`
  prints the `vllm` commands without running them. This is the recipe `just bench`
  drives on the bench host.
- **`load-cell`** — run one `vllm bench serve` cell: its coordinate (`share`,
  `burstiness`, and an optional `max_concurrency` cap) and the shared knobs
  (lengths, SLO, seed) both come from a cell YAML passed as `--config` (default
  `bench/cell.yaml`), and one result JSON is written. The orchestrator renders one
  such cell per grid point from a `load-sweep` config; this is the grain the
  bench-client container executes now that the per-cell loop lives in the
  orchestration layer ([`docs/adr/0012`](docs/adr/0012-sweep-resumability-and-orchestrator-choice.md)
  §Amendment).
- **`sweep-grid`** — emit one validated slice (`engine-points`,
  `concurrency-ladder`, `burstiness`) of the knob grid in `bench/sweep-grid.yaml`
  for `just knob-sweep` to read, so every swept value is validated up front and
  nothing is hard-coded in the recipe (ADR-0009).
- **`aggregate-sweep`** — fold a finished `knob-sweep` run's per-point result JSON
  into the concurrency-ceiling table, the durable artifact of the sweep.
- **`cost`** — price result JSON files (path arguments) into $/1M input and output
  tokens from a `--config` provenance YAML: the instance price/hr and output:input
  ratio, tagged with the weight checksum, vLLM version and quantization recipe that
  produced the run.
- **`commercial-cost`** — price the same tokens at a commercial API's quoted $/1M
  input and output rates, read from a `--config` provenance YAML pinning the api,
  model and quote date; the comparison arm for the scoreboard.
- **`prefix-cache`** — compute one run's prefix-cache hit-rate from the `/metrics`
  snapshots bracketing it and the cell JSON, tagged by `--cache-state cold|warm`
  (which regime the run measured). This is the join `just prefix-cache` runs locally
  for each regime after the host produces the snapshots.
- **`report`** — join the three arms (latency, cost, commercial) into the baseline
  $/1M-at-SLO report, emitted as JSON or a Markdown table (`--format`).
- **`chart`** — chart a finished `knob-sweep` run: write the ceiling and goodput-cliff
  tables (Markdown + JSON, the durable artifacts) and their plots under
  `<run-dir>/charts` (ADR-0009).

Prerequisites: `uv` (>= 0.5). `uv run` provisions the virtualenv from
`uv.lock` on first use — no manual `venv` or `pip install`. Every subcommand takes
`--help` for its full option list.

```sh
uv run slipstream-bench --help              # list the subcommands
uv run slipstream-bench load-sweep --help   # options for one subcommand
just cli-test                               # run the package test suite (uv run pytest)
```

Runtime dependencies are `typer` (CLI), `pydantic` + `pyyaml` (validate the
`--config` YAMLs), `prometheus-client` (parse `/metrics`), `pandas` + `seaborn`
(the `chart` tables and plots), and `prefect` + `prefect-aws` (the sweep cache/retry
layer, [`docs/adr/0012`](docs/adr/0012-sweep-resumability-and-orchestrator-choice.md));
`pytest` and `ruff` are dev-only, and the repo's
pre-commit `ruff` / `ruff-format` hooks lint the package. The tools
run inside a baked bench-client image on an external EC2 bench host, which drives
them against vLLM through an ephemeral public mutual-TLS load balancer, so the
latency recorded is what an off-cluster client sees. That measurement path is
recorded in [`docs/adr/0004`](docs/adr/0004-bench-vantage-external-path.md).

Prefect's result and cache-key storage point at S3, not its local
`~/.prefect/storage/` default: `slipstream_bench.orchestration.storage` builds two
`S3Bucket` blocks under distinct prefixes (`prefect/results`, `prefect/cache-keys`)
of the same `RESULTS_BUCKET` the sweep already writes to, with AWS credentials from
the ambient default chain. Resume survives a server or laptop death only with both
on S3 — the hinge recorded in
[`docs/adr/0012`](docs/adr/0012-sweep-resumability-and-orchestrator-choice.md).

The bench-client image (`bench/Dockerfile`) layers the package and the model
tokenizer onto the same pinned vLLM engine build the server runs, so the two
tokenize identically. The tokenizer is baked from the `bench_model` var — the GPU
rig's model from `model.yaml` — which `bench` and `prefix-cache` also send as the
sweep model, so the image tokenizer and the sweep never diverge:

```sh
just bench-image   # docker build + push the bench-client image to ECR
```

### Bench image CI

`just bench-image` is for a local, pinned build; merges to `main` publish the
image. The [`bench-image`](.github/workflows/bench-image.yml) workflow
authenticates to ECR via GitHub OIDC (no long-lived keys), and its build is
content-hash idempotent: it computes the image's content tag, checks ECR, and
builds and pushes only when that tag is absent. It publishes two tags — the
immutable `<slug>-<sha>` content tag and the floating `<slug>-main` pointer — and
is the sole publisher of `-main`. A pull request instead gets a build-only,
no-push Dockerfile check in [`ci.yml`](.github/workflows/ci.yml), so a broken
Dockerfile fails the no-cloud gate without any credentials. The scheme is
recorded in [`docs/adr/0008`](docs/adr/0008-ci-built-bench-image-and-model-definition.md).

One-time setup: the workflow reads the push role's ARN, the target ECR
repository name, and the region from repository variables, all fed from the
bootstrap stack's outputs so they track the infrastructure terraform created.
After `just bootstrap` has applied the OIDC role and repository (see #101), set
them once — they are repo-global, not per-branch:

```sh
gh variable set AWS_BENCH_IMAGE_PUSH_ROLE_ARN \
  --body "$(terraform -chdir=terraform/bootstrap output -raw bench_image_push_role_arn)"
gh variable set ECR_REPOSITORY \
  --body "$(terraform -chdir=terraform/bootstrap output -raw bench_image_repo_url | cut -d/ -f2-)"
gh variable set AWS_REGION \
  --body "$(terraform -chdir=terraform/bootstrap output -raw region)"
```

Bootstrapping under a different repo (a fork, a rename, or a new owner) also
needs the OIDC trust to match that repo. The trust subject uses GitHub's
immutable owner/repo IDs, so set `github_oidc_sub_prefix` in
`terraform/bootstrap` to the repo's own value before applying — the default is
this repo's IDs, and a mismatch fails the workflow's role assumption with
`Not authorized to perform sts:AssumeRoleWithWebIdentity`:

```sh
gh api repos/OWNER/REPO/actions/oidc/customization/sub --jq .sub_claim_prefix
# -> repo:<owner>@<owner_id>/<repo>@<repo_id>, the value for github_oidc_sub_prefix
```

To benchmark an unmerged branch's image: pull requests never publish, so first
push the branch's content image locally, then pin the endpoint to that immutable
tag rather than the `-main` default (which tracks the last `main` publish, not
your branch):

```sh
just bench-image   # build + push this checkout's qwen3-8b-awq-<sha> content tag
terraform -chdir=terraform/bench-endpoint apply \
  -var bench_image_tag="$(bench/image-tag.sh sha-tag)"
```

### Benchmark runbook

A benchmark run stands up an ephemeral, internet-facing endpoint and an EC2 host,
then tears them down. The sequence, from a cold checkout:

```sh
just bootstrap        # once per environment: ECR repo + shared state (see ADR-0005)
just cluster-up       # create the cluster, point kubectl at it
just gpu-pool-up      # apply the GPU node pool + NVIDIA device plugin
just gpu-deploy       # roll out the GPU vLLM replica (Karpenter brings up the g5)
just bench-image      # build + push the bench-client image to ECR

just bench-endpoint-up      # stand up the mutual-TLS load balancer + bench host
just bench                             # sweep the prefix-share x burstiness grid
just prefix-cache 90 1.0               # cold/warm hit-rate for one cell
just bench-endpoint-down    # tear down the endpoint, host, certs and client secret
```

`just bench-endpoint-up` reads the operator's public IP from `checkip.amazonaws.com` and
pins the load balancer's security group to that `/32`; mutual TLS is the real gate,
the `/32` a second lock. `just bench` and `just prefix-cache` drive the host over
SSM Run Command — the host has no inbound access — and sync each run's results under
`bench/results/` (`bench/results/<run-id>/` for `just bench`,
`bench/results/prefix-cache/<run-id>/` for `just prefix-cache`; pull runs made from
another machine with `just bench-results-sync`). Both recipes require a live bench endpoint.

The endpoint is public and billed while up: **run `just bench-endpoint-down` as soon as a
run finishes.** `bench-endpoint-down` leaves the cluster and vLLM running; `just cluster-down`
destroys the cluster. Neither touches the bootstrap state bucket or the ECR repo.

Region is not a per-run flag: the benchmark recipes read it from the Terraform
outputs (`terraform output -raw region`), so the cluster, the bench endpoint stack and
the AWS CLI all act in one region. Set it once via `AWS_PROFILE` / `aws configure`; a
profile pointing at a different region than the state was created in will not find
these resources.

The whole stack — cluster, GPU pool, GPU replica, bench endpoint — comes up with one
recipe and tears down with another, for when you want it live to run sweeps against
through the day:

```sh
just stack-up         # cluster-up + gpu-pool-up + gpu-deploy + bench-endpoint-up
just bench                             # run sweeps against the live rig, any time
just stack-down       # bench-endpoint-down + gpu-down + gpu-pool-down + cluster-down, then leak sweep (spend → 0)
```

`stack-down` returns all GPU and cluster spend to zero, then runs the same
zero-leak sweep as `cloud-verify` (below) to confirm no `Project=slipstream`
instance or volume — including a Karpenter g5 the destroy raced — is still
billing; it exits non-zero if one survived. Only the bootstrap state bucket and
ECR repo survive. `cluster-down` is gated on `bench-endpoint-down` succeeding:
the endpoint's load balancer and host sit in the cluster VPC, so tearing the VPC
down under a live endpoint would wedge on a dependency violation and orphan a
billing load balancer — a failed endpoint teardown stops the run before that.

### Knob sweep runbook

`just knob-sweep` is the two-tier engine-knob sweep (ADR-0009). Every swept value
lives in [`bench/sweep-grid.yaml`](bench/sweep-grid.yaml), read through
`slipstream-bench sweep-grid`, which validates the whole grid before the first
deploy — the recipe holds no knob values of its own.

- **Tier 1 — engine knobs**, one GPU redeploy per point: `max-num-seqs` ×
  KV-cache dtype (`fp8`/`fp16`) × prefix caching (on/off). The knobs live in the
  vLLM launch args, so each combination needs a fresh `gpu-deploy`.
- **Tier 2 — client load ladder**, no redeploy: against each running Tier-1 point,
  `just bench` walks the concurrency ladder to find the highest rung that holds
  goodput at the SLO — that point's sustained concurrency ceiling.

It runs against a live stack, so bring the GPU rig and the bench endpoint up first:

```sh
just stack-up          # cluster + GPU pool + GPU replica + bench endpoint
just knob-sweep        # redeploy + drive the ladder per point (long-running)
just stack-down        # tear the rig down; spend → 0
```

Each point's ladder JSON lands under `bench/results/<run-id>/<point>/`, with the
per-point predicted ceilings tabulated alongside. A point that fails to deploy or
whose ladder reports failures is recorded and the sweep continues, exiting non-zero
at the end (its resumability and orchestrator choice are recorded in
[`docs/adr/0012`](docs/adr/0012-sweep-resumability-and-orchestrator-choice.md)).
Turn a finished run into artifacts locally — no cluster needed:

```sh
uv run slipstream-bench aggregate-sweep --run-dir bench/results/<run-id>   # ceiling table
uv run slipstream-bench chart --run-dir bench/results/<run-id>             # tables + plots
```

### Cloud verify runbook

`just cloud-verify` is the money-safety backstop for the GPU path (ADR-0002 item 4):
one hand-triggered, real-GPU-spend end-to-end check that the rig serves and that
teardown returns spend to zero. It costs real money, so a human triggers it — there
is no automatic cloud run (ADR-0002).

It expects a clean slate: it aborts if the eks stack already holds any resource,
since running against a live cluster turns `cluster-up` into a multi-minute
node-group roll and would tear down a cluster you did not stand up here. Run
`just stack-down` first, then rerun.

```sh
just cloud-verify     # cluster-up → gpu-pool-up → gpu-deploy → smoke → teardown → sweep
```

It runs the full path and asserts two things:

1. **Harness green** — the GPU replica answers `/v1/completions` with HTTP 200 and a
   well-formed, non-empty completion (`just gpu-completion`). This proves the deploy
   → service → engine → token path; it does not judge answer quality (spec §5).
2. **Zero-leak** — after tearing the path down (`gpu-down` → `gpu-pool-down` →
   `cluster-down`), it sweeps EC2 for any `Project=slipstream` instance or volume still in a
   billing state and asserts none remain. Karpenter's `g5` and its `gp3` root live
   outside Terraform state, so `just cluster-down` never sees them; the `EC2NodeClass` tags
   them with the same `Project` tag the Terraform stacks carry so the sweep
   (`test/zero-leak-sweep.sh`, classified by `slipstream-bench zero-leak`) can find
   a leaked node the destroy missed. The sweep covers EC2 instances and EBS volumes
   only — the Karpenter g5 and its gp3 root are the resources that live outside
   Terraform state; `cluster-down` (`terraform destroy`) owns everything else. A
   future out-of-state resource of another kind would need adding to the sweep.

Teardown and the sweep run **even if the smoke fails**, so a failed check never
leaves a live `g5` billing. The recipe exits non-zero if the smoke failed, a
teardown step failed, or a tagged resource survived — a green exit means the rig
served and spend is back at zero.

## Non-goals

No fine-tuning or LLM training, no RAG / vector DBs / embeddings, no agent
frameworks, no Ray Train (Ray Serve is in scope), no multi-cloud or portability
abstractions, not a real product — this is a platform demonstrator.
