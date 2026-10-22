<div align="center">

<img src="docs/logo.svg" alt="slipstream" width="400">
<br><br>

[![ci](https://github.com/pvardanis/slipstream/actions/workflows/ci.yml/badge.svg)](https://github.com/pvardanis/slipstream/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![ruff](https://img.shields.io/badge/ruff-0.16.7-D7FF64?logo=ruff&logoColor=black)](https://github.com/astral-sh/ruff)
[![ty](https://img.shields.io/badge/ty-0.0.84-261230)](https://github.com/astral-sh/ty)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![terraform](https://img.shields.io/badge/terraform-%E2%89%A51.11-7B42BC?logo=terraform&logoColor=white)](https://developer.hashicorp.com/terraform)
[![vLLM](https://img.shields.io/badge/vLLM-0.29.0-30A2FF)](https://github.com/vllm-project/vllm)
[![platform](https://img.shields.io/badge/platform-AWS%20EKS-FF9900)](#architecture)

</div>

A self-hosted LLM inference platform on AWS EKS. Today it serves an AWQ-quantized open
model with vLLM on a Karpenter-provisioned `g5` GPU node and benchmarks it from outside
the cluster. An unattended two-tier knob sweep (engine knobs × client concurrency)
finds each configuration's sustained concurrency ceiling at an SLO, and a KV cache A/B
measures what offloading KV cache to a CPU tier (LMCache) adds on top of the GPU prefix
cache. `just stack-up` / `just stack-down` conjure and destroy the whole rig. Like the
low-drag wake it is named after, every layer exists to move more tokens per dollar.

The build spec is [`docs/spec.md`](docs/spec.md); decisions are recorded in
[`docs/adr/`](docs/adr/) and terms in [`CONTEXT.md`](CONTEXT.md).

## Architecture

```mermaid
---
config:
  elk:
    nodePlacementAlignment: LEFTUP
---
flowchart TB
    op([Operator])

    subgraph gha[GitHub Actions]
        ci[ci.yml<br/>lint, tests, no-push builds]
        img[bench-image /<br/>orchestration-image]
        disp[sweep-dispatch /<br/>kv-ab-dispatch]
    end

    subgraph aws[AWS]
        awslogo@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/aws-cloud.svg", label: "", pos: "b", w: 32, h: 32, constraint: "on" }
        ecr@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/ecr.svg", label: "ECR", pos: "b", w: 48, h: 48, constraint: "on" }
        subgraph eks[EKS]
            ekslogo@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/eks.svg", label: "", pos: "b", w: 32, h: 32, constraint: "on" }
            vllm[vLLM on g5<br/>Karpenter,<br/>optional LMCache CPU tier]
            ps@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/prefect/prefect.svg", label: "Prefect server", pos: "b", w: 48, h: 48, constraint: "on" }
            pw[Prefect worker]
        end
        alb@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/alb.svg", label: "mTLS ALB", pos: "b", w: 48, h: 48, constraint: "on" }
        host@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/ec2-instance.svg", label: "Bench host", pos: "b", w: 48, h: 48, constraint: "on" }
        s3@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/s3-bucket.svg", label: "S3 results", pos: "b", w: 48, h: 48, constraint: "on" }
    end

    op -- push / PR --> ci
    op -- merge to main --> img
    op -- workflow_dispatch --> disp
    op -- just stack-up / down --> eks
    img -- OIDC push --> ecr
    disp -- OIDC + port-forward:<br/>register + run --> ps
    ps --> pw
    pw -- redeploy per point<br/>or arm run --> vllm
    pw -- SSM: one docker run per cell --> host
    ecr -. image .-> host & pw
    host -- load --> alb --> vllm
    host -- result JSON --> s3
    pw -- "charts, A/B records,<br/>Prefect results + cache keys" --> s3
```

The CI/CD pipeline and the benchmark data path are detailed in
[`docs/architecture.md`](docs/architecture.md).

## Prerequisites

| Tool | Version | Used for |
| --- | --- | --- |
| `terraform` | >= 1.11 | all infrastructure |
| `just`, `kubectl`, `aws` CLI | — | recipes, cluster access, SSM |
| `uv` | >= 0.5 | Python workspace, `prefect` CLI |
| `docker` | — | local bench-image builds |
| `prek`, `tflint` | — | git hooks |
| `gh` | — | setting repo variables (`just gh-vars`) |

On macOS (Homebrew core ships a frozen terraform 1.5.7, so use HashiCorp's tap):

```sh
brew install hashicorp/tap/terraform just tflint awscli kubectl uv prek gh   # + Docker Desktop
```

Optional: `k9s`, `stern`, `kubens` for inspecting the `slipstream` namespace.

## Setup

### Local environment

```sh
uv sync          # workspace members + dev tools (pytest, ruff, ty)
prek install     # git hooks: ruff, terraform fmt/tflint, shellcheck, shfmt, actionlint
just cli-test    # local suite, no cluster
```

### AWS access

Work from a dedicated IAM user, not the account root: root cannot hold an EKS access
entry. Create the user (VPC/EKS permissions, console access + MFA), give it an access
key, and sign in to the console as that user to inspect EKS there. Then:

```sh
aws configure --profile slipstream-admin   # access key + region (eu-west-1)
export AWS_PROFILE=slipstream-admin        # the only variable you set by hand
```

`just cluster-up` grants cluster-admin to the caller, so `kubectl` works immediately.
The recipes read the region from Terraform outputs; a profile pointing at another region
will not find the resources.

### New environment or fork

Once per AWS account / repository. The image workflows need the repo variables, and
`just cluster-up` deploys the Prefect worker from the published orchestration image,
hence the order:

1. In a fork or renamed repo, set `github_oidc_sub_prefix` in `terraform/bootstrap` to
   the repo's OIDC subject prefix, otherwise every workflow fails with
   `Not authorized to perform sts:AssumeRoleWithWebIdentity`:
   ```sh
   gh api repos/OWNER/REPO/actions/oidc/customization/sub --jq .sub_claim_prefix
   ```
2. `just bootstrap` — remote-state bucket, ECR repositories, GitHub OIDC roles.
3. `just gh-vars` — sets the eight repo variables the workflows read from the
   bootstrap outputs.
4. Dispatch `bench-image` and `orchestration-image` on `main` to publish the images.
   ECR keeps them across `just stack-down`, so this is once per environment.

## Quickstart

```sh
just stack-up             # cluster + Prefect + GPU pool + vLLM + bench endpoint
# trigger a knob sweep: Actions → sweep-dispatch → Run workflow (on main)
# or a KV cache A/B:    Actions → kv-ab-dispatch → Run workflow (on main)
just bench-results-sync   # pull results into bench/results/ — before stack-down
just stack-down           # tear down, then zero-leak sweep (spend → 0)
```

The bench endpoint is public and every node bills while up: **run `just stack-down`
when done.** Only the state bucket and ECR repositories survive it; the results bucket
is deleted with its contents, so sync first. `stack-down` exits non-zero if a tagged
instance or volume survived, and skips `cluster-down` (leaving the cluster billing) if
the endpoint teardown failed: fix that and rerun. Running a sweep locally, single
points, pinned images and the cloud-verify check are in
[`docs/operations.md`](docs/operations.md); `uv run slipstream-bench --help` lists the
bench CLI.

## Recipes

`just` (or `just --list`) lists every recipe, grouped by concern
([ADR-0013](docs/adr/0013-justfile-imported-lifecycle-modules.md)):

| Group | Covers |
| --- | --- |
| `cluster` | Remote-state bootstrap, EKS up/plan/down, GPU node pool, repo variables |
| `prefect` | Work pool, deployment registration, UI port-forward (`prefect-ui`), storage drain |
| `serve` | vLLM api-key Secret, GPU replica deploy/scale, completion smokes |
| `bench` | Bench-client image, one point's ladder, prefix-cache, results sync, mTLS endpoint |
| `obs` | OTel Collector spine stub |
| `test` | Python suite and shell unit tests (no cluster) |
| `orchestrate` | Whole-stack up/down, cloud verification |

## Roadmap

Planned in [`docs/spec.md`](docs/spec.md) §4, not yet built:

- **L2a** — autoscaling on inference signals and cold-start elimination
- **L2b** — spot-eviction survival mid-stream
- **L3** — inference-aware routing
- **L4** — prefill/decode disaggregation

## Deliberate trade-offs

The rig is one GPU on a duty-cycle budget, and most of what it does not do follows
from that:

- **One long-lived worker, not a pod per run.** The sweep runs on a single Prefect
  process worker (one replica, work-pool concurrency 1) that lives as long as the
  cluster. A Kubernetes Job per flow run buys isolation, per-run resources, and parallel
  autoscale, none of which help a serial sweep on one static GPU node. A GPU fleet that
  makes engine points truly parallel is the trigger to switch to a kubernetes pool
  ([ADR-0015](docs/adr/0015-unattended-sweep-cloud-server-and-parent-flow.md)).
- **On-demand GPU, not spot.** [`docs/spec.md`](docs/spec.md) plans for spot, but the
  node pool is on-demand only and capped at one GPU: allowing spot cost ~15 min of
  launch thrash in a spot-starved region, where on-demand launches in ~90s
  ([`k8s/gpu-node-pool.yaml`](k8s/gpu-node-pool.yaml)).
- **No always-on control plane.** Prefect Cloud's Starter tier (~$100/mo, the cheapest
  that allows an own-compute worker) or a standing self-hosted stack would add an idle
  cost floor. The Prefect server is self-hosted on SQLite inside
  the cluster and is torn down with it; S3 holds the results and resume state.
- **A duty cycle, not 24/7.** The GPU budget is ~$100–250/mo, while the cheapest 24 GB
  card run continuously is ~$365/mo, so `just stack-down` returns spend to zero between
  sweeps.
- **One sample per cell.** Each knob-sweep cell is measured once; re-sampling is a
  deliberate new `run_id`, never an average taken by default
  ([ADR-0012](docs/adr/0012-sweep-resumability-and-orchestrator-choice.md)). The KV cache
  A/B is the exception: it compares two arms, so each arm is run `runs_per_arm` times,
  interleaved, and reported as median and range.
- **No apply/destroy infra tests per push.** CI runs policy checks and offline
  `terraform test` against mocked providers; the real-spend check, `just cloud-verify`,
  runs by hand ([ADR-0002](docs/adr/0002-infrastructure-testing-strategy.md)).
- **No high availability.** One CPU node, one Karpenter controller, and one Prefect
  server: HA needs a second node, and SQLite on a single-attach EBS volume allows one
  writer. A dead pod fails the in-flight run: Kubernetes restarts the pod, not the run,
  and running it again with the same `run_id` resumes from the cells already measured.
- **No standing endpoint, no laptop runs.** The mutual-TLS load balancer and bench host
  exist only for a run, and recorded numbers never come from a laptop, whose Wi-Fi
  jitter would inflate the p95/p99 tail
  ([ADR-0004](docs/adr/0004-bench-vantage-external-path.md)).

## Non-goals

No fine-tuning or LLM training, no RAG / vector DBs / embeddings, no agent
frameworks, no Ray Train (Ray Serve is in scope), no multi-cloud or portability abstractions, not a real product — this is a
platform demonstrator.
