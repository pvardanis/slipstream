<!-- ADR recording how CI decides what to run from the paths a change touches: one `changes` job in ci.yml classifies a change as docs-only and flags which image inputs it touches, heavy jobs skip on docs-only changes, a single `ci-ok` gate is the only required status check, the orchestration image's PR build-and-scan lives in one reusable workflow, the publish workflows ignore docs-only pushes, and a shell test keeps the duplicated path lists in sync. -->

# ADR-0027: CI gates its jobs on the paths a change touches

- Status: Accepted
- Date: 2026-11-03

## Context

`ci.yml` ran every job on every pull request and every push to `main`: prek, pytest, four
offline terraform stacks, the shell unit tests and the Helm chart render. A change that
touched only the README took the same five to six minutes as a change to the package
source, and no test reads a committed doc.

Branch protection listed seven required checks by job name. The list had drifted from the
workflow (`terraform tests (offline) (grafana)`, `helm chart render (kubeconform)` and the
orchestration image build were not required), and a matrix job skipped by a job-level `if`
reports under its unexpanded name, so the per-stack required checks would sit pending
forever on a run that skipped them.

The orchestration image was built twice on each pull request that touched its inputs: once
by `ci.yml` (with a `kubectl` assert) and once by `orchestration-image.yml` (with a Trivy
scan), from the same path list.

`bench-image.yml` and `orchestration-image.yml` ran on every push to `main`. Both are
content-hash idempotent, so a docs-only push built nothing, but still checked out, took an
OIDC role and probed ECR.

The image input path lists lived in three places each (the PR filter, the content-tag
pathspec and the workflow trigger), kept in step by comments alone.

## Decision

### One `changes` job classifies the change

A `changes` job in `ci.yml` runs first and emits three outputs:

- `docs_only`: every changed file matches the docs denylist, `docs/**`, `**/*.md`,
  `reports/**` or `.claude/skills/**`.
- `bench_image`: the change touches a bench image input (the `bench/image-tag.sh` pathspec).
- `orchestration_image`: the change touches an orchestration image input (the
  `orchestration-image.yml` content-tag pathspec).

A denylist rather than per-job allowlists: pytest reads configs from `bench/`, `terraform/`
and `k8s/`, so an allowlist per job would be one more hand-synced list that silently skips
a test when it misses a path. The denylist can only err towards running.

`.claude/hooks/` is not in the denylist: it holds shell.

### Heavy jobs skip on docs-only changes; lint always runs

pytest, the terraform matrix, the shell unit tests and the chart render skip when
`docs_only` is true. prek always runs: its whitespace, end-of-file and YAML hooks check the
docs too. The image builds skip at the job level when their output is false, so an
untouched image starts no runner. The same gating applies to pushes to `main`, which keep
running `ci.yml` so the README badge reflects `main`.

### `ci-ok` is the only required check

A `ci-ok` job needs every other job, runs with `if: always()`, and fails when any of them
failed or was cancelled. A skipped job passes. Branch protection requires `ci-ok` alone, so
a new job is covered by adding it to `needs`, with no change to the protection settings.

### The orchestration image's PR build and scan is one reusable workflow

`orchestration-image-scan.yml` (`on: workflow_call`) builds the image, scans it with Trivy
and asserts `kubectl` is on its `PATH`. `ci.yml` calls it on pull requests behind
`orchestration_image`, so `ci-ok` covers it. `orchestration-image.yml` calls it on its
weekly schedule, and keeps the publish job on pushes to `main`.

### The publish workflows ignore docs-only pushes

`bench-image.yml` and `orchestration-image.yml` carry the docs denylist as `paths-ignore`
on their `push` trigger. Every other push to `main` still publishes when its content tag
is absent from ECR.

### A shell test keeps the path lists in sync

`test/ci-paths-parity-test.sh` reads the lists with `yq` and fails when they differ: the
docs denylist across `ci.yml` and both publish workflows, the bench image filter against
`bench/image-tag.sh`, and the orchestration image filter against the content-tag pathspec
in `orchestration-image.yml`.

## Alternatives considered

- **Workflow-level `paths-ignore` on `ci.yml`.** A required check from a workflow that
  never runs stays pending, so a docs-only pull request could not merge.
- **Per-job path allowlists.** They save more runner time, at the cost of a test skipped
  whenever a list misses an input. See the denylist rationale above.
- **Dropping `push: main` from `ci.yml`.** Strict branch protection already tests the merged
  tree, but the repository is public, so the post-merge run costs nothing, blocks no one,
  and keeps the badge live.

## Consequences

- A docs-only pull request runs `changes`, prek and `ci-ok`.
- Switching branch protection to `ci-ok` is a one-time manual step after the gate merges.
- A new docs location must be added to the denylist in all three workflows, and the parity
  test fails until it is.
