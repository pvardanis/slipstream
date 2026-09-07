<!-- ADR recording the task-runner and remote-state-bootstrap choices for provisioning EKS. -->

# ADR-0001: `just` task runner and a separate state-bootstrap stack

- Status: Accepted
- Date: 2026-09-07

## Context

Layer 1a stands up a minimal EKS cluster with Terraform only — the AWS console is
never touched. Two decisions shape how it is driven and where its state lives:

1. **What drives `up`/`down`.** The spec and glossary describe an `up`/`down` verb
   pair that fully creates and destroys capacity so idle time costs nothing. That
   verb pair needs a task runner.
2. **Where remote state lives.** Terraform needs durable, locked, shared state, but
   the state store cannot be created by the stack that depends on it — the
   chicken-and-egg bootstrap problem.

## Decision

**Task runner: `just`.** A `justfile` at the repo root exposes `just up` and
`just down`.

- `up`: `terraform init` → `apply -auto-approve` → `aws eks update-kubeconfig` →
  `kubectl get nodes` smoke check.
- `down`: `terraform destroy -auto-approve`.

`just` is chosen over Make because the recipes are ordered command lists, not
file-dependency graphs; Make's build-oriented model (targets, `.PHONY`, implicit
rules) adds ceremony with no payoff here.

**Remote state: a separate `terraform/bootstrap/` stack.** It creates one
versioned, encrypted S3 bucket with public access blocked and `prevent_destroy`
set. It runs with **local state committed to git** and is applied once, by hand,
before the first `just up`. The main `terraform/eks/` stack uses the S3 backend
with the **native S3 lockfile** (`use_lockfile = true`) — no DynamoDB table.

Native locking is GA as of Terraform 1.11, so both stacks pin
`required_version >= 1.11`.

## Consequences

- `just down` returns cluster spend to zero; the bootstrap bucket is deliberately
  excepted (it holds the state that makes teardown reversible).
- The backend block cannot take variables, so the bucket name and region are
  literals in `terraform/eks/backend.tf` that must match the bootstrap defaults.
  Changing one means changing both.
- Bootstrap state is committed. It contains only S3-bucket metadata — no secrets —
  so committing it is safe and keeps the bootstrap reproducible.
- No DynamoDB lock table to provision, pay for, or clean up. The tradeoff is that
  native S3 locking is single-object; that is sufficient for a single-operator
  project.
