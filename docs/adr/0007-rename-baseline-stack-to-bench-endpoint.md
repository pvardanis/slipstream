<!-- ADR renaming the baseline Terraform stack to bench-endpoint, aligning the folder, state key, resource addresses and cross-stack references with the bench-endpoint vocabulary the recipes already use. -->

# ADR-0007: Rename the baseline stack to bench-endpoint

- Status: Accepted
- Date: 2026-09-21

## Context

The `just` recipes and their comments already call the ephemeral exposure stack
the **bench endpoint** (`bench-endpoint-up`, `bench-endpoint-down`), but the
Terraform stack still lived at `terraform/baseline/` with a `baseline`
state key, an `aws_lb.baseline` resource, `baseline`-prefixed names and tags, and
`baseline` in the CI matrix, Dependabot config and cross-stack comments. The term
had moved; the stack had not, leaving two names for one thing.

"baseline" carries a second, unrelated meaning in this repo: the **benchmark
baseline** — the reference measurement the cost and latency arms compare against
(`src/slipstream_bench/report.py`, `commercial_cost.py`, the `docs/spec.md`
"apples-to-apples or it is not a baseline" rule). That vocabulary is correct and
predates the stack. A blanket rename would have corrupted it.

## Decision

**Rename the exposure stack to `bench-endpoint` wherever `baseline` names the
stack, endpoint, or its resources; leave `baseline` untouched wherever it names
the benchmark measurement.**

Renamed (stack identity):

- `terraform/baseline/` → `terraform/bench-endpoint/`
- state key `baseline/terraform.tfstate` → `bench-endpoint/terraform.tfstate`
- `aws_lb.baseline` → `aws_lb.bench_endpoint`; name prefixes `bsln-` → `bench-`;
  `${cluster_name}-baseline` name and `Component = "baseline"` tag →
  `bench-endpoint`; the cert SAN `vllm.baseline.slipstream.internal` →
  `vllm.bench-endpoint.slipstream.internal`
- justfile `baseline_dir` → `bench_endpoint_dir`
- CI matrix `stack: [baseline, eks]` → `[bench-endpoint, eks]`; Dependabot
  `/terraform/baseline` → `/terraform/bench-endpoint`
- stack-sense comments in the eks/bootstrap outputs, the k8s manifests, and the
  README

Kept (benchmark measurement): the `baseline` vocabulary in `src/slipstream_bench/`,
`docs/spec.md`, the report arm, and run/metric-sense comments ("a baseline run",
"the baseline latency it records").

**The state key change needs no state migration**: no bench-endpoint stack was
deployed at the time of the rename. The stack is ephemeral by design — created on
`bench-endpoint-up`, destroyed on `bench-endpoint-down` — so the next `init`
starts fresh state under the new key with nothing to strand.

## Consequences

- The cloud-verify leak sweep filters on `Project=slipstream`, not `Component`,
  so the `Component` tag rename does not affect it.
- A `bench-endpoint-up` run after this change provisions resources under the new
  names and writes state to the new key. Had a stack been live, the old-key state
  and its resources would have been orphaned; this rename is safe only because
  nothing was deployed.
- One name for the stack across recipes, Terraform, CI, and docs. The benchmark
  baseline keeps its own name, so the two concepts no longer share a word.
