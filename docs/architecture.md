# Architecture

Two views of the running system: how code reaches AWS, and how the knob sweep and the
KV cache A/B measure the GPU replica. The target architecture (routing, autoscaling, P/D
split) is in [`spec.md`](spec.md) §9.

## CI/CD

```mermaid
---
config:
  elk:
    nodePlacementAlignment: LEFTUP
---
flowchart LR
    pr[Pull request] --> ci
    main[Push to main] --> ci & bimg & oimg
    op([Operator]) -- workflow_dispatch<br/>on main --> sdisp & kdisp

    subgraph gha[GitHub Actions]
        ci[ci.yml<br/>prek, pytest, terraform test,<br/>shell tests, no-push image builds]
        bimg[bench-image.yml]
        oimg[orchestration-image.yml]
        sdisp[sweep-dispatch.yml]
        kdisp[kv-ab-dispatch.yml]
        disp[flow-dispatch.yml<br/>reusable body]
    end

    subgraph aws[AWS]
        awslogo@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/aws-cloud.svg", label: "", pos: "b", w: 32, h: 32, constraint: "on" }
        brole@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/iam-role.svg", label: "bench push role", pos: "b", w: 48, h: 48, constraint: "on" }
        orole@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/iam-role.svg", label: "orchestration push role", pos: "b", w: 48, h: 48, constraint: "on" }
        drole@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/iam-role.svg", label: "sweep-dispatch role", pos: "b", w: 48, h: 48, constraint: "on" }
        ecr@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/ecr.svg", label: "ECR", pos: "b", w: 48, h: 48, constraint: "on" }
        subgraph eks[EKS]
            ekslogo@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/eks.svg", label: "", pos: "b", w: 32, h: 32, constraint: "on" }
            ps@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/prefect/prefect.svg", label: "Prefect server", pos: "b", w: 48, h: 48, constraint: "on" }
        end
    end

    sdisp & kdisp -- workflow_call --> disp
    bimg -- OIDC --> brole --> ecr
    oimg -- OIDC --> orole --> ecr
    disp -- OIDC --> drole
    ekslogo ~~~ ps
    drole -- port-forward --> ps
    disp -- register + run<br/>knob-sweep or<br/>kv-cache-extension-ab --> ps
```

- **No long-lived keys.** Every AWS call assumes an OIDC role created by
  `terraform/bootstrap`; no workflow reads a secret. All three roles trust only `main`
  of this repo (`github_oidc_sub_prefix`).
- **Image publishing is content-addressed.** Each image workflow computes the image's
  content tag, skips the build if ECR already has it, and otherwise pushes the content
  tag plus a floating pointer (`<slug>-main` for the bench client, `main` for the
  orchestration image), which only `main` CI publishes
  (`just bench-image` pushes a content tag from a workstation). Pull requests get a
  no-push build in `ci.yml` ([ADR-0008](adr/0008-ci-built-bench-image-and-model-definition.md)).
- **One dispatch body, two triggers.** `sweep-dispatch` and `kv-ab-dispatch` each call
  `flow-dispatch.yml`, which looks up the flow's deployment and config parameter in one
  table, registers the deployment, and starts a run. A called workflow runs in its
  caller's context and mints the caller's OIDC token, so both triggers assume the same
  role under the same `main`-only trust.
- **Flow dispatch is least-privilege.** The role can describe the cluster and bench
  host and list buckets. An EKS access entry maps it to a Kubernetes group that can only
  read and port-forward within the Prefect namespace (`terraform/eks`).
- **Repo variables** (`just gh-vars`, from the bootstrap outputs): `AWS_REGION`,
  `ECR_REPOSITORY`, `AWS_BENCH_IMAGE_PUSH_ROLE_ARN`, `ORCHESTRATION_ECR_REPOSITORY`,
  `AWS_ORCHESTRATION_IMAGE_PUSH_ROLE_ARN`, `AWS_SWEEP_DISPATCH_ROLE_ARN`,
  `EKS_CLUSTER_NAME`, `PREFECT_NAMESPACE`.

## Benchmark data path

```mermaid
---
config:
  elk:
    nodePlacementAlignment: LEFTUP
---
flowchart TB
    subgraph eks[EKS]
        ekslogo@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/eks.svg", label: "", pos: "b", w: 32, h: 32, constraint: "on" }
        ps@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/prefect/prefect.svg", label: "Prefect server", pos: "b", w: 48, h: 48, constraint: "on" }
        ebs@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/ebs-gp3-volume.svg", label: "EBS", pos: "b", w: 48, h: 48, constraint: "on" }
        pw[Prefect worker<br/>Pod Identity: S3 + SSM]
        vllm[vLLM replica<br/>g5.xlarge via Karpenter<br/>LMCache CPU tier when offload is on]
    end

    subgraph endpoint[bench-endpoint stack]
        alb@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/alb.svg", label: "ALB, mutual TLS", pos: "b", w: 48, h: 48, constraint: "on" }
        host@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/ec2-instance.svg", label: "Bench host, no inbound", pos: "b", w: 48, h: 48, constraint: "on" }
        s3@{ img: "https://raw.githubusercontent.com/pvardanis/slipstream/main/docs/icons/aws/s3-bucket.svg", label: "results bucket", pos: "b", w: 48, h: 48, constraint: "on" }
    end

    ps -- SQLite --> ebs
    ps -- flow run --> pw
    pw -- "Tier 1:<br/>kubectl redeploy<br/>per engine point or A/B arm run" --> vllm
    pw -- "Tier 2: SSM" --> host
    host -- vllm bench serve --> alb --> vllm
    host -- "result JSON<br/>knob-sweep/run-id/point-slug/,<br/>kv-offload-ab/run-id/arm-rN/" --> s3
    pw -- "charts<br/>knob-sweep/run-id/charts/,<br/>kv-offload-ab/run-id/charts/" --> s3
    pw -- "A/B rung labels<br/>kv-offload-ab/run-id/arm-rN/*.kv_ab.json" --> s3
    pw -- "Prefect results + cache keys<br/>prefect/results/, prefect/cache-keys/" --> s3
    pw -- ceiling and spread tables<br/>as run artifacts --> ps
```

- **External vantage.** The bench host sits outside the cluster and reaches vLLM
  through a public mutual-TLS load balancer, so recorded latency is what an off-cluster
  client sees ([ADR-0004](adr/0004-bench-vantage-external-path.md)). The bench-client
  image runs the same pinned vLLM build and tokenizer as the server.
- **Two tiers.** Tier 1 redeploys the GPU replica once per engine point
  (`max-num-seqs` × KV-cache dtype × prefix caching). Tier 2 walks a client concurrency
  ladder against that point to find the highest rung that holds goodput at the SLO
  ([ADR-0009](adr/0009-knob-sweep-rig-and-ceiling-method.md)). All values come from
  [`bench/configs/sweep-grid.yaml`](../bench/configs/sweep-grid.yaml).
- **Resumable within a run.** Each cell is a Prefect task keyed by its run id, config
  digest, engine point, and cell, with results and cache keys on S3. The cache record
  decides whether a cell is complete: rerunning the same run id skips any cell whose
  config is unchanged, and a new run id measures every cell fresh. The redeploy-skip gate
  reads the same records, so a point redeploys only when one of its cells will run
  ([ADR-0012](adr/0012-sweep-resumability-and-orchestrator-choice.md),
  [ADR-0022](adr/0022-run-id-scoped-resume-on-prefects-cache.md)).

- **KV cache A/B.** A second flow, `kv-cache-extension-ab`, on the same worker and pool
  (concurrency 1, so it queues behind a sweep). It measures KV cache offload off against
  on at one engine point, with prefix caching on in both arms, and caps the GPU KV pool
  with `--kv-cache-memory-bytes` so the workload's working set overflows it. Arm runs are
  interleaved (off, on, off, on). Each arm run that has a rung to measure redeploys the
  replica, which clears both cache tiers, runs an unrecorded warmup, then the concurrency
  ladder. Every rung checks
  the KV connector's counters (`vllm:external_prefix_cache_*`) against its arm, and its
  record carries the GPU and combined prefix-cache hit rates. Resume is per whole arm run.
  The workload is [`bench/configs/kv-cache-extension-ab.yaml`](../bench/configs/kv-cache-extension-ab.yaml);
  the refusals and results are in [`operations.md`](operations.md#kv-cache-ab), and the
  decisions in [ADR-0023](adr/0023-kv-cache-extension-cpu-tier-and-ab.md).

## Terraform stacks

Applied in this order. `grafana`, `eks` and `bench-endpoint` read bootstrap outputs through
remote state; `bench-endpoint-up` passes the eks network outputs in as variables, so teardown
needs no live cluster.

| Stack | Lifetime | Holds |
| --- | --- | --- |
| `terraform/bootstrap` | permanent | state bucket, ECR repositories, GitHub OIDC roles |
| `terraform/grafana` | permanent | Grafana Cloud CloudWatch data source |
| `terraform/eks` | `cluster-up` → `cluster-down` | VPC, EKS, Karpenter, EBS storage, Prefect server and worker, sweep-dispatch RBAC |
| `terraform/bench-endpoint` | `bench-endpoint-up` → `bench-endpoint-down` | mTLS ALB, certs, bench host, results bucket |

The GPU node pool (`k8s/gpu-node-pool.yaml`) and vLLM replica (`k8s/vllm-gpu.yaml`) are
applied with `kubectl` between the eks and bench-endpoint stacks.
