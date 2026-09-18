<!-- ADR moving the bootstrap stack's own state from committed local storage to the remote S3 backend, superseding the state-storage decision in ADR-0001. -->

# ADR-0005: Bootstrap stack keeps its state in S3, not committed to git

- Status: Accepted
- Date: 2026-09-14
- Supersedes: the "Remote state" storage decision in [ADR-0001](0001-task-runner-and-state-bootstrap.md)

## Context

ADR-0001 stood up a separate `terraform/bootstrap/` stack that creates the S3
bucket the `terraform/eks/` stack stores its state in, plus the bench-client ECR
repository. To break the chicken-and-egg — the state store cannot live in a bucket
it has not created yet — bootstrap ran with **local state committed to git**,
reasoning the state held only bucket metadata, no secrets, so committing it was
safe and kept the bootstrap reproducible.

In practice the committed state is a recurring cost. Every `apply` or `refresh`
bumps `serial` and normalises provider attributes (e.g. an ECR `tags: null → {}`
drift), producing a git diff on `terraform.tfstate` that has nothing to do with any
source change. State is a live record, not source; carrying it in git means every
infra operation dirties the working tree and invites those diffs into unrelated
commits.

## Decision

**Bootstrap keeps its own state in the same S3 bucket it creates**, at key
`bootstrap/terraform.tfstate`, with the native S3 lockfile — the same backend the
eks stack already uses. `terraform/bootstrap/backend.tf` names the bucket as a
**literal**, not a `-backend-config` lookup: bootstrap is the root of the state
chain and cannot resolve its own bucket dynamically without a circular init. The
bucket carries `prevent_destroy`, so the name is durable.

The committed `terraform.tfstate` is removed from git and the tree-wide
`*.tfstate` ignore (the `!terraform/bootstrap/terraform.tfstate` exception is
dropped). No state is tracked in git anywhere.

**The existing environment was cut over once** (bucket already existed):

```bash
AWS_PROFILE=<profile> terraform -chdir=terraform/bootstrap init -migrate-state
```

Terraform copied the committed local state into S3 and `terraform plan` then
reported no changes. This ran once, against the committed local state this ADR
removes; it is not repeatable from a fresh clone — there is no local state to
migrate, and `terraform init` alone connects to the state already in S3.

**Bootstrapping a brand-new environment** (bucket does not exist yet) is a
one-time, three-command sequence, because `init` cannot reach a bucket that is
not there:

```bash
terraform -chdir=terraform/bootstrap init -backend=false   # providers only, local state
terraform -chdir=terraform/bootstrap apply -auto-approve    # creates the bucket + ECR
terraform -chdir=terraform/bootstrap init -migrate-state     # move state into the new bucket
```

`just bootstrap` covers only the steady case (bucket exists); the greenfield
two-step is run by hand.

## Consequences

- Infra operations no longer dirty the working tree; there is no committed state to
  diff, and no state in git history going forward.
- The eks stack still resolves the bucket name from the bootstrap output
  (`terraform -chdir=terraform/bootstrap output -raw state_bucket_name`), which now requires
  bootstrap to be initialised against its remote backend. `cluster-up`, `plan`, and the
  image recipes gain a `_bootstrap-init` dependency so a fresh checkout can read
  those outputs.
- The tradeoff ADR-0001 took the other way: committed state made a fresh clone
  self-contained and the bootstrap trivially reproducible from git. Now the state
  lives only in S3 (versioned, encrypted). Losing it means re-running the
  greenfield two-step against the existing bucket — cheap, because bootstrap
  manages only the bucket and the ECR repo — but it is a real reduction in
  clone-and-go reproducibility, accepted to keep state out of git.
- Greenfield bootstrap is now a documented manual sequence rather than a single
  `just bootstrap`. Acceptable: it happens once per AWS account.
