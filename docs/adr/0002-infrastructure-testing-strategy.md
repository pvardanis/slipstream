<!-- ADR recording how infrastructure (Terraform, k8s manifests, just recipes) is tested, and what is deliberately not tested, for a solo budget-constrained demonstrator. -->

# ADR-0002: Infrastructure testing strategy

- Status: Accepted
- Date: 2026-09-10

## Context

The platform is Terraform, Kubernetes manifests, and `just` bash recipes. It
needs a testing approach proportionate to a solo, duty-cycle, budget-constrained
demonstrator — not an enterprise one. Two facts about this project fix what
testing is for:

1. **The product is the repo.** The spec's definition of done (§7) is public
   evidence — a repo with honest commit history — not a running cluster. A
   silently regressed decision (someone flips a Service to `LoadBalancer`,
   un-pins an image tag) erodes the thing being demonstrated.
2. **The only real-money failure is a botched teardown.** The GPU budget is a
   duty-cycle budget (spec §2); `just down` returning spend to zero is a design
   requirement. The mechanical failure is concrete: a `Service type=LoadBalancer`
   or a `PersistentVolumeClaim` creates an ELB or EBS volume through *in-cluster
   controllers*, which live outside Terraform state. `just down` runs
   `terraform destroy` and never sees them, so they orphan and keep billing.

Testing is therefore ranked: **money-safety first, decision-credibility second,
developer velocity third** (velocity is already served by the existing prek
hooks). These priorities are organised along two axes — test *content* by the
blast radius of the failure it guards (leaks spend / regresses a documented
decision / breaks the dev loop), and test *execution* by the money boundary
(what is free to run per-push versus what spins real AWS).

Three framework facts constrain the design (verified against current docs):

- `terraform test` with `command = plan` runs without applying (free) and can
  assert planned attribute values and outputs. With `command = apply` it creates
  real infrastructure, asserts, then destroys — real cost.
- `prevent_destroy` **cannot be asserted by `terraform test` at all.** It is a
  `lifecycle` meta-argument handled by Terraform core; it never appears in the
  plan JSON or state schema, so no assertion can reference it. Guarding it
  requires static analysis of the HCL source.
- `conftest` is a single static binary that parses both YAML and HCL, roughly
  five lines of Rego per rule, and drops into prek.

## Decision

A two-tier strategy, split by the money boundary.

**No-cloud tier (free, per-push, the GitHub Actions gate):**

1. **Static/schema** — already in prek: `terraform fmt`/`validate`/`tflint`,
   `kubeconform`, `shellcheck`/`shfmt`.
2. **Policy, via conftest/OPA** — one engine, two policy sets:
   - Over `k8s/*.yaml`: `Service.type != LoadBalancer` and no `PersistentVolumeClaim`
     (the teardown-leak *prevention* — the leak becomes unauthorable), plus
     resource limits present, probes present, image tag pinned (no `:latest`).
   - Over `terraform/**/*.tf`: presence checks for the literal invariants that a
     plan-test cannot meaningfully guard — `prevent_destroy` on the state bucket,
     bucket versioning/SSE/public-access-block lines intact.
3. **`terraform test`, plan-only** — assertions over *computed* values only
   (subnet CIDR math, AZ count, node-group count), where plan logic can actually
   break. Literal pass-through attributes are guarded by the policy set above,
   not here.

**Cloud tier (real AWS, label-gated on the PR, never automatic):**

4. **Teardown-verify** — a `just` recipe (and a `cloud-check`-labelled workflow)
   that runs `up` → `down` → an `aws` sweep for tagged leftovers, asserting zero.
   This is the money-safety backstop.
5. **`completion` smoke** — the existing recipe is made to assert rather than
   print: HTTP 200 **and** a well-formed, non-empty completion. It proves the
   deploy → service → engine → token path end-to-end. It does not assert answer
   quality (out of scope per spec §5).

**Triggering.** The no-cloud tier runs per-push/PR (free). The cloud tier runs
only when a PR carries the `cloud-check` label, so it gates *before* merge and
costs money only when an infra or manifest change warrants it. There is no
auto-trigger on merge and no scheduled cloud run.

**Deliberately deferred.** No standing Terratest or automated `command = apply`
CI loop. A full apply/destroy EKS loop per push would burn the budget the spec
protects, and the money-failure it would catch is already prevented at the
policy layer; the label-gated backstop covers the residual. Deferred until a
regression actually bites.

**Ship order and ceiling.** Policy (item 2) and the `completion` assert (item 5)
ship first — they serve both priorities and are cheap. The teardown-verify
backstop (item 4) follows. Plan-only `terraform test` (item 3) is the lowest
value and the first to cut. Infra-testing effort is capped at roughly the prek
setup already spent; past that it is plumbing, not testing.

## Consequences

- The priority-one failure (teardown leak) is guarded mainly in the *free* tier,
  by prevention: a `LoadBalancer` Service or PVC cannot be authored. The
  expensive detection path exists only as a deliberate, hand-triggered backstop.
- `prevent_destroy` on the state bucket is guarded by static HCL policy, not by
  `terraform test` — the framework cannot assert the meta-argument, and an ADR
  that claimed otherwise would be false confidence.
- The plan-only `terraform test` tier is intentionally thin. Asserting literals
  that sit in the HCL unchanged is a change-detector, not a bug-catcher, so that
  work moves to static policy and the tier keeps only assertions over computed
  plan values.
- `main` stays green on infra because the cloud gate is pre-merge and label-gated;
  the trade-off is that the gate depends on a human remembering the label. A path
  filter could nudge this later, but automatic cloud spend is rejected.
- The `completion` smoke proves the infrastructure path, not the model. Conflating
  "the model answered well" with "the infra is correct" is avoided by asserting
  only shape and liveness.
- conftest adds one binary dependency and Rego as a policy language. This is
  accepted over a bash/`yq` script because the manifest surface is known to grow
  across phases L2–L4 (Karpenter/KEDA, the IGW gateway, node-pool topology), and
  a string-matching script rots against CRDs where Rego scales.
- Each piece ships as its own PR — this prose ADR alone, then the policy set,
  then the `completion` change, then the workflow — one kind of change each.
