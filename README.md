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
`AWS_PROFILE` that can create VPC/EKS resources.

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
cluster. [`k8s/README.md`](k8s/README.md) explains the manifest and the `kubectl`
commands field by field.

## Non-goals

No fine-tuning or LLM training, no RAG / vector DBs / embeddings, no agent
frameworks, no Ray Train (Ray Serve is in scope), no multi-cloud or portability
abstractions, not a real product — this is a platform demonstrator.
