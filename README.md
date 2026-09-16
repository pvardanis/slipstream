# slipstream

A self-hosted LLM inference platform on AWS EKS: open models served on spot GPUs,
with the cluster-level machinery around the model as the focus — autoscaling on
inference signals, spot-eviction survival mid-stream, cold-start elimination on
large weight loads, inference-aware routing, and prefill/decode disaggregation.
`just up` / `just down` conjures and destroys GPU capacity. It runs
cost-competitive against commercial APIs and proves it on a live scoreboard.

The name carries the platform's job: a slipstream is the low-drag wake behind a
fast-moving object. Every layer here reduces drag — prefix caching, KV-cache
offload, cold-start removal, prefill/decode split — so more tokens travel per
dollar. Token streaming and speculative decoding (a draft model riding the big
model's slipstream) sit literally inside the word.

## Status

Spec landed: [`docs/spec.md`](docs/spec.md) — build-ready, detailed enough to start
Layer 0. It was charted as a wayfinder map on this repo's issues (label
`wayfinder:map`); the decision log at the end of the spec traces every choice back to
its ticket.

## Provisioning

All infrastructure is Terraform; the AWS console is never touched. A `justfile`
drives it. Credentials come from your `AWS_PROFILE` — Terraform never handles them.

Prerequisites: `terraform` (>= 1.11), `just`, `kubectl`, and the AWS CLI, with an
`AWS_PROFILE` that can create VPC/EKS resources (see [AWS access](#aws-access)).
The benchmark recipes also need `docker` (to build the bench-client image) and
`envsubst` (from GNU gettext, to render its image reference into the pod
manifest). Optional inspection tools: `k9s`, `stern`, `kubens`.

```sh
just bootstrap   # one-time: create the S3 remote-state bucket
just up          # create the cluster, then `kubectl get nodes`
just deploy      # run the CPU vLLM replica and wait for it to serve
just completion  # port-forward the service and curl a completion out of it
just down        # destroy the cluster; spend returns to zero
```

`just bootstrap` runs once (its state is committed local state). After that,
`just up` / `just down` are the cluster lifecycle. The task-runner and
state-bootstrap choices are recorded in
[`docs/adr/0001`](docs/adr/0001-task-runner-and-state-bootstrap.md).

`just deploy` applies [`k8s/vllm.yaml`](k8s/vllm.yaml): a single vLLM replica
serving a tiny CPU model (`Qwen/Qwen2.5-0.5B-Instruct`) over the OpenAI API, so
the platform stands up without spending GPU hours. The service is `ClusterIP`
only — no public endpoint, no cloud load balancer — so `just completion` reaches
it through `kubectl port-forward`, and `just down` tears the cluster down with
nothing left behind. `just undeploy` removes the workload without destroying the
cluster.

### AWS access

Work from a **dedicated IAM user, not the account root**. Root can't be granted
an EKS access entry, so the console's EKS **Resources** tab shows `Unauthorized`
for root and the cluster only trusts whoever ran `just up`.

1. As root (one time), create an IAM user (e.g. `slipstream-admin`) with the
   permissions to manage VPC/EKS, and enable **console access** + MFA for it.
2. Give it an **access key**, then configure a local profile:
   ```sh
   aws configure --profile slipstream-admin   # paste the access key + region (eu-west-1)
   export AWS_PROFILE=slipstream-admin         # the profile just/terraform/kubectl use
   ```
3. Sign in to the AWS console **as that user** (not root) at
   `https://<account-id>.signin.aws.amazon.com/console` to inspect EKS there.

`just up` grants cluster-admin to this caller automatically
(`enable_cluster_creator_admin_permissions`), so `kubectl` works immediately.

### Inspecting the cluster

The workload lives in the `slipstream` namespace (created by `just deploy`).

```sh
kubens slipstream                     # set the default namespace (no more -n flags)
k9s                                   # live TUI: pods, logs (l), describe (d), events
stern vllm -n slipstream --tail 50    # tail vLLM logs, follows pod restarts
```

`k9s` is the fastest way to see pod status and events; `stern` beats
`kubectl logs` during a crash loop because it re-attaches to each new pod.

## Benchmark harness

The L0 benchmark harness ships as `slipstream-bench`, a Python package managed by
[`uv`](https://docs.astral.sh/uv/). Its `typer` CLI dispatches the three benchmark
tools — `serve-sweep` (fire `vllm bench serve` across a prefix-share x burstiness
grid), `cost` (price a run into $/1M in/out tokens), and `prefix-cache` (the
cold/warm hit-rate delta). The rewrite from bash is recorded in
[`docs/adr/0003`](docs/adr/0003-l0-bench-harness-in-python.md).

Prerequisites: `uv` (>= 0.5). `uv run` provisions the virtualenv from
`uv.lock` on first use — no manual `venv` or `pip install`.

```sh
uv run slipstream-bench --help   # list the three subcommands
uv run slipstream-bench cost --help
just cli-test                    # run the package test suite (uv run pytest)
```

Runtime dependencies stay slim (`typer` and `prometheus-client`); `pytest` and `ruff` are dev-only,
and the repo's pre-commit `ruff` / `ruff-format` hooks lint the package. The tools
run inside a baked bench-client image on an external EC2 bench host, which drives
them against vLLM through an ephemeral public mutual-TLS load balancer, so the
latency recorded is what an off-cluster client sees. That measurement path is
recorded in [`docs/adr/0004`](docs/adr/0004-bench-vantage-external-path.md).

The bench-client image (`bench/Dockerfile`) layers the package and the model
tokenizer onto the same pinned vLLM engine build the server runs, so the two
tokenize identically. It is pushed to an ECR repository provisioned by
`just bootstrap`; the bench host pulls it with the host IAM role. Build and push
it before a sweep (rebuild after changing the package or the base engine):

```sh
just bench-image                 # docker build + push to ECR
```

### Baseline runbook

A benchmark run stands up an ephemeral, internet-facing endpoint and an EC2 host,
then tears them down. The sequence, from a cold checkout:

```sh
just bootstrap        # once per environment: ECR repo + shared state (see ADR-0005)
just up               # create the cluster, point kubectl at it
just deploy           # roll out the CPU vLLM replica, wait for it to serve
just bench-image      # build + push the bench-client image to ECR

just baseline-up      # stand up the mutual-TLS load balancer + bench host
just bench                             # sweep the prefix-share x burstiness grid
just prefix-cache 90 1.0               # cold/warm hit-rate for one cell
just baseline-down    # tear down the endpoint, host, certs and client secret
```

`just baseline-up` reads the operator's public IP from `checkip.amazonaws.com` and
pins the load balancer's security group to that `/32`; mutual TLS is the real gate,
the `/32` a second lock. `just bench` and `just prefix-cache` drive the host over
SSM Run Command — the host has no inbound access — and sync each run's results under
`bench/results/` (`bench/results/<run-id>/` for `just bench`,
`bench/results/prefix-cache/<run-id>/` for `just prefix-cache`; pull runs made from
another machine with `just bench-results-sync`). Both recipes require a live baseline.

The endpoint is public and billed while up: **run `just baseline-down` as soon as a
run finishes.** `baseline-down` leaves the cluster and vLLM running; `just down`
destroys the cluster. Neither touches the bootstrap state bucket or the ECR repo.

Region is not a per-run flag: the benchmark recipes read it from the Terraform
outputs (`terraform output -raw region`), so the cluster, the baseline stack and
the AWS CLI all act in one region. Set it once via `AWS_PROFILE` / `aws configure`; a
profile pointing at a different region than the state was created in will not find
these resources.

## Non-goals

No fine-tuning or LLM training, no RAG / vector DBs / embeddings, no agent
frameworks, no Ray Train (Ray Serve is in scope), no multi-cloud or portability
abstractions, not a real product — this is a platform demonstrator.
