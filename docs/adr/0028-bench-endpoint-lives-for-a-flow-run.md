<!-- ADR recording that the bench endpoint lives for a flow run: the knob sweep and KV cache A/B flows each apply the bench-endpoint stack from the Prefect worker when they start, wait for the bench host to boot, run their cells against it, and destroy the stack when they end, so the operator passes no instance id and `just stack-up` brings up no endpoint. Covers the worker's bounded infra rights, the baked Terraform, the unattended apply with no saved plan, the refusal to touch an endpoint the run did not create, the state shared with the manual recipes, and what a killed or cancelled run leaves behind. Amends ADR-0004 (lifecycle) and ADR-0015 (worker rights). -->

# ADR-0028: The bench endpoint lives for a flow run

- Status: Accepted
- Date: 2026-11-06

## Context

The bench endpoint (ADR-0004) is the mutual-TLS public load balancer in front of vLLM,
together with the bench host: the EC2 instance outside the cluster that runs the
bench-client image and drives the cells over SSM. `just stack-up` stood it up and
`just stack-down` tore it down, so it billed and stayed publicly reachable for the whole
working session. The two unattended Prefect flows (knob sweep and KV cache A/B) need it
only while they run; `just bench` and `just prefix-cache` use it ad hoc.

Each flow run also took the bench host's instance id, which the operator read from the
stack's outputs and CI found by EC2 tags.

## Decision

**Each unattended flow owns its bench endpoint.** The knob sweep opens a bench-endpoint
session around its point loop, and the KV cache A/B around its arm runs, through one
opener (`flows.run_setup.open_run_bench_endpoint`). The session applies
`terraform/bench-endpoint`, reads the bench host's instance id from the stack's outputs,
waits for the host's boot sentinel in the results bucket, yields the bench host to the
run, and destroys the stack on the way out. The operator triggers a run with its run id
alone, from a workstation, `sweep-dispatch` or `kv-ab-dispatch`, and the run stays
unattended. `just stack-up` brings up no endpoint.

- **The session is Prefect-free** (`slipstream_bench.orchestration.bench_endpoint`).
  Terraform is injected as a typed `Callable` over argv, the same shape as the `Kubectl`
  transport, so its tests drive it against a fake Terraform and a fake S3 client. It logs a
  milestone for each step (applying, launched, ready after N seconds, destroying,
  destroyed), which the flow routes to the run page.
- **Once the apply starts, the destroy always runs** while the flow's process can unwind. A
  failed apply, a host that reports `failed: <step>`, a host that does not report ready
  within 420 s, and a raising body all end in a destroy. A failed destroy fails the run
  before the results are rendered, and its error names the state and
  `just bench-endpoint-down`.
- **A run never touches an endpoint it did not create.** Before applying, the session reads
  the state, and if it already holds resources it refuses with the state's location and
  `just bench-endpoint-down`. It never force-unlocks the state. One state key means one
  endpoint at a time, and the sweep pool's concurrency limit of 1 (`just prefect-up`) keeps
  two runs from racing for it.
- **The apply runs unattended, with no saved plan.** `init`, `apply -auto-approve
  -input=false` and `destroy -auto-approve` run against the same state key the manual
  recipes use (`bench-endpoint/terraform.tfstate`). This departs from reviewed plans on
  purpose: the stack is fixed and covered by tftests, and its inputs come from the eks
  stack. Rebuilding the stack in boto3 was rejected because it would throw away the tested
  stack and lose destroy-from-state.
- **The orchestration image bakes the Terraform the run uses:** a pinned Terraform, the
  stack, and its providers pre-initialised in a filesystem mirror, so a run never reaches
  the Terraform registry. `BENCH_ENDPOINT_STACK_DIR` names the baked stack.
- **The eks stack hands the worker everything the apply needs.** The state bucket, the
  cluster topology (VPC, public subnets, node security group, node autoscaling groups) and
  the bench host's instance profile come from the `bench-endpoint-topology` ConfigMap, as
  `BENCH_ENDPOINT_*` variables in the worker's environment. The vLLM api-key is read at run
  time from the `slipstream/vllm-api-key` Secret and reaches Terraform through its
  environment, never argv.
- **The worker's infra rights are bounded by the stack**
  (`terraform/eks/worker_bench_endpoint.tf`). It gets `iam:PassRole` on the bench host role
  and no IAM writes, so it cannot escalate to admin. Creating and deleting the stack's
  resources is scoped by its name prefixes or its `Component=bench-endpoint` tag; describes
  are account-wide, the node security group rule and the ASG attachment are scoped by the
  cluster's tags, and the ACM tag bounds intent rather than enforcing it. In the state
  bucket it writes only the bench-endpoint key and its lock, and reads the bootstrap state.
  Its Kubernetes RBAC adds `get` on the one api-key Secret.

`just bench` and `just prefix-cache` read the bench host from the stack's outputs, so they
run against an endpoint brought up by hand with `just bench-endpoint-up`; the point sweep
keeps its instance-id parameter so it can target such an endpoint.

## Consequences

- The endpoint bills and is publicly reachable only while a flow runs, plus the minutes
  the apply, the host's boot and the destroy take, or while one `just bench-endpoint-up`
  stood up is up.
- The flows and the manual recipes share one state. A flow refuses to start while an
  endpoint from `just bench-endpoint-up` is up, and running `just bench-endpoint-down` or
  `just stack-down` during a flow destroys its endpoint, or fails on the state lock while
  the flow is applying or destroying.
- A KV cache A/B whose workload the rig rules out (`check_fits_rig`) fails before it
  applies anything.
- A resume under the same run id brings up a new bench host. No cache key or digest
  includes the instance id, so cells that already hold a cache record are still reused
  (ADR-0022). A resume whose every cell is cached still applies and destroys an endpoint;
  skipping that is not worth the extra path.
- The destroy runs in a `finally`, so a killed run skips or interrupts it. A worker pod
  killed outright (OOM, node reaped) skips it. A run cancelled from the UI gets SIGTERM
  across its process group, Terraform included, and SIGKILL 30 s later: the destroy starts
  but a load balancer and an instance do not come down in that window. Either way the
  endpoint stays up and billing, and a kill mid-apply or mid-destroy also leaves the state
  lock held. The next run refuses the state; `just bench-endpoint-down` clears it, after
  `terraform -chdir=terraform/bench-endpoint force-unlock <lock-id>` if the lock is held
  (the id is in Terraform's lock error) and the run is Cancelled or Crashed with its
  worker pod gone, since a live apply or destroy holds the same lock. Resources a killed apply had not yet written to
  state are invisible to Terraform. The spend-leak guard's fast alarm watches GPU capacity
  only (ADR-0025), so a leaked bench endpoint shows up only against the monthly budget.
- When the body fails and the destroy then fails too, the run's error is the destroy's; the
  body's error is chained beneath it in the traceback.
- The worker can create and destroy public-facing AWS resources, within the bounds above,
  which the tftests pin.
