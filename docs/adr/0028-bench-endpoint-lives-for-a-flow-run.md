<!-- ADR recording that the bench endpoint lives for a flow run: the knob sweep flow applies the bench-endpoint stack from the Prefect worker when it starts, waits for the bench host to boot, runs its cells against it, and destroys the stack when it ends, so the operator passes no instance id. Covers the worker's bounded infra rights, the baked Terraform, the unattended apply with no saved plan, the refusal to touch an endpoint the run did not create, and what a killed or cancelled run leaves behind. Amends ADR-0004 (lifecycle) and ADR-0015 (worker rights). The KV cache A/B adopts the same session in #471. -->

# ADR-0028: The bench endpoint lives for a flow run

- Status: Accepted
- Date: 2026-11-06

## Context

The bench endpoint (ADR-0004) is the mutual-TLS public load balancer in front of vLLM,
together with the bench host: the EC2 instance outside the cluster that runs the
bench-client image and drives the cells over SSM. `just stack-up` stands it up and
`just stack-down` tears it down, so it bills and stays publicly reachable for the whole
working session. The two unattended Prefect flows (knob sweep and KV cache A/B) need it
only while they run; `just bench` and `just prefix-cache` use it ad hoc.

Each flow run also took the bench host's instance id, which the operator read from the
stack's outputs and CI found by EC2 tags.

## Decision

**The knob sweep flow owns its bench endpoint.** It opens a bench-endpoint session around
its point loop: the session applies `terraform/bench-endpoint`, reads the bench host's
instance id from the stack's outputs, waits for the host's boot sentinel in the results
bucket, yields the bench host to the run, and destroys the stack on the way out. The
operator triggers a run with no instance id, from a workstation or `sweep-dispatch`, and
the run stays unattended.

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

The point sweep keeps its instance-id parameter, so `just bench` still runs against an
endpoint brought up by hand with `just bench-endpoint-up`. The KV cache A/B still drives a
running bench host and takes its instance id, and `just stack-up` still creates the
endpoint, until the A/B opens the same session (#471).

## Consequences

- The endpoint bills and is publicly reachable only while a knob sweep runs, plus the
  minutes the apply, the host's boot and the destroy take.
- `just stack-up` leaves an endpoint up, and a knob sweep refuses to start against it. Run
  `just bench-endpoint-down` before a knob sweep until #471 lands.
- A resume under the same run id brings up a new bench host. No cache key or digest
  includes the instance id, so cells that already hold a cache record are still reused
  (ADR-0022). A resume whose every cell is cached still applies and destroys an endpoint;
  skipping that is not worth the extra path.
- The destroy runs in a `finally`, so a run whose process is killed skips or interrupts it:
  a worker pod killed outright (OOM, node reaped), or a run cancelled from the UI, which
  the worker ends by terminating the flow's process. The endpoint stays up and billing, and
  a kill mid-apply or mid-destroy also leaves the state lock held. The next run refuses the
  state; `terraform force-unlock <lock-id>` (the lock id is in Terraform's lock error) and
  then `just bench-endpoint-down` clear it. Resources a killed apply had not yet written to
  state are invisible to Terraform. The spend-leak guard's fast alarm watches GPU capacity
  only (ADR-0025), so a leaked bench endpoint shows up only against the monthly budget.
- When the body fails and the destroy then fails too, the run's error is the destroy's; the
  body's error is chained beneath it in the traceback.
- The worker can create and destroy public-facing AWS resources, within the bounds above,
  which the tftests pin.
