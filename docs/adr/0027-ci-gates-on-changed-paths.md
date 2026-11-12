<!-- ADR recording how CI decides what to run from the paths a change touches: one `changes` job in ci.yml classifies a change as docs-only and flags which image inputs it touches, failing closed when a result is missing; test jobs skip on docs-only changes; a single `ci-ok` gate is the only required status check; markdown is excluded from the image inputs; the orchestration image's PR build-and-scan lives in one reusable workflow; the publish workflows ignore docs-only pushes; and a shell test, run in lint, checks the gate's path lists and wiring. -->

# ADR-0027: CI gates its jobs on the paths a change touches

- Status: Accepted
- Date: 2026-11-03

## Context

`ci.yml` ran every test job on every pull request and every push to `main`: prek, pytest,
four offline terraform stacks, the shell unit tests and the Helm chart render. A change
that touched only the README took the same five to six minutes as a change to the package
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

The orchestration image's input paths lived in three places (the `ci.yml` PR filter, the
content-tag pathspec and `orchestration-image.yml`'s `pull_request` paths), and the bench
image's in two, kept in step by comments alone.

## Decision

### One `changes` job classifies the change

A `changes` job in `ci.yml` runs before the gated jobs and emits three outputs:

- `docs_only`: every changed file matches the docs denylist, `docs/**`, `**/*.md`,
  `reports/**` or `.claude/skills/**`.
- `bench_image`: the change touches a bench image input (the `bench/image-tag.sh` pathspec).
- `orchestration_scan`: the change touches an orchestration image input (the
  `orchestration/content-tag.sh` pathspec) or the scan's configuration
  (`.trivyignore` and the scan workflow), which can change the scan's verdict without
  changing the image.

A denylist rather than per-job allowlists: pytest reads configs from `bench/`, `terraform/`
and `k8s/`, so an allowlist per job would be one more hand-synced list that silently skips
a test when it misses a path. A path the denylist does not name always runs the tests.
`reports/**` and `.claude/skills/**` cover whole directories, which hold only markdown and
images today; a script added under either would skip the tests.

`.claude/hooks/` is not in the denylist: it holds shell.

Each output fails closed: it reads the filter's explicit `false`, so a missing result (a
misnamed filter, or a manual dispatch, which skips the filter) is never docs-only and
always counts as touching the images. A manual dispatch therefore runs every test job.

### Test jobs skip on docs-only changes; lint always runs

pytest, the terraform matrix, the shell unit tests and the chart render skip when
`docs_only` is true. prek always runs: its whitespace, end-of-file and YAML hooks check the
docs too. The image builds stay PR-only, and skip at the job level when their output is
false, so an untouched image starts no runner. The `docs_only` gating also applies to
pushes to `main`, which keep running `ci.yml` so the README badge reflects `main`.

### Markdown is not an image input

Both content-tag pathspecs exclude `**/*.md`, and both image filters exclude it to match. A
change the gate reads as docs-only therefore never rolls a content tag, so skipping its PR
build and its publish never leaves a content tag with no image behind it.

### `ci-ok` is the only required check

A `ci-ok` job needs every other job, runs with `if: always()`, and fails when any of them
failed or was cancelled. A skipped job passes. Branch protection requires `ci-ok` alone, so
a new job is covered by adding it to `needs`, with no change to the protection settings.

### The orchestration image's PR build and scan is one reusable workflow

`orchestration-image-scan.yml` (`on: workflow_call`) builds the image, scans it with Trivy
and asserts `kubectl` is on its `PATH`. `ci.yml` calls it on pull requests behind
`orchestration_scan`, so `ci-ok` covers it. `orchestration-image.yml` calls it on its
weekly schedule, and keeps the publish job on pushes to `main`.

### The publish workflows ignore docs-only pushes

`bench-image.yml` and `orchestration-image.yml` carry the docs denylist as `paths-ignore`
on their `push` trigger. Every other push to `main` still publishes when its content tag
is absent from ECR. GitHub matches only the first 3,000 changed files of a push, so a push
whose first 3,000 are all docs skips the publish even with code beyond them, and the
floating pointer lags until the next push that is not all docs.

### A shell test checks the gate

`test/ci-paths-parity-test.sh` reads the workflows with `yq` and `bench/image-tag.sh` with
`awk`, and fails when:

- the docs denylist differs across `ci.yml` and both publish workflows;
- the bench image filter differs from `bench/image-tag.sh`, or the orchestration image
  filter from the content-tag pathspec in `orchestration/content-tag.sh`;
- the `code` filter includes anything narrower than everything;
- `scan_config` does not name `.trivyignore` and the scan workflow `ci.yml` calls;
- an output reads a filter that does not exist, or fails open on a missing result;
- `ci-ok` does not need every other job, or does not run `always()`.

It then runs the same checks against mutated copies of the workflows and
`bench/image-tag.sh`, and each copy must fail the check it targets, so a change to the
parsing that silently empties or skips a check fails the test. It runs in the lint job, which never skips:
the shell unit tests skip on a docs-only change, which is the change a broken gate would
misread.

## Alternatives considered

- **Workflow-level `paths-ignore` on `ci.yml`.** A required check from a workflow that
  never runs stays pending, so a docs-only pull request could not merge.
- **Per-job path allowlists.** They save more runner time, at the cost of a test skipped
  whenever a list misses an input. See the denylist rationale above.
- **Dropping `push: main` from `ci.yml`.** Strict branch protection already tests the merged
  tree, but the repository is public, so the post-merge run costs nothing, blocks no one,
  and keeps the badge live.
- **Detecting the markdown overlap in the test instead of excluding it.** It would catch an
  overlapping file only once committed; the exclusion makes the overlap impossible.

## Consequences

- A docs-only pull request runs `changes`, prek and `ci-ok`.
- Switching branch protection to `ci-ok` is a one-time manual step after the gate merges.
- A new docs location must be added to the denylist in all three workflows; the parity
  test fails while they differ.
- The test checks that the duplicated lists agree, not that they are complete: a new image
  input missing from both a filter and its pathspec passes it.
