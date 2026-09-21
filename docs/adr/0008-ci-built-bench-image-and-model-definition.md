<!-- ADR moving the bench-client image build into CI on an OIDC-authenticated GitHub workflow, replacing the mutable `latest` tag with a tokenizer-slugged content-hash scheme, and introducing models.yaml as the single source of truth for the served model that the bench image bakes its tokenizer from. -->

# ADR-0008: CI-built bench image, tokenizer-identified tags, and a model-definition source of truth

- Status: Accepted
- Date: 2026-09-21

## Context

The bench-client image (`bench/Dockerfile`) bakes the tokenizer of the served
model so client and server tokenize identically. It was built and pushed to ECR
only by hand, from a developer machine, via `just bench-image`. Nothing rebuilt
it when its inputs changed, and there was no build at all until someone ran the
recipe.

Two things obscured what a given image contained:

- The tag was the literal `latest` (`justfile`, `bench_image_tag` in
  `terraform/bench-endpoint`), reused on every push against a `MUTABLE` ECR repo.
  The tag said nothing about which tokenizer was inside.
- The served model id and its HF revision were hardcoded in `k8s/vllm-gpu.yaml`
  and *duplicated* in the justfile's `gpu_model` var, kept in sync by hand — a
  comment in the justfile admits the manual sync. The tokenizer the bench image
  bakes and the model vLLM serves must match, but nothing enforced it.

CI (`.github/workflows/ci.yml`) is a deliberate no-cloud gate (ADR-0002 tier 1):
`contents: read`, no AWS credentials. Building and pushing to ECR needs cloud
credentials, which that gate must not hold.

## Decision

**Build the bench image in CI on a separate, OIDC-authenticated workflow;
identify each image by a tokenizer-slugged content tag; and make the served
model a single source of truth that both the image build and (later) the serving
manifest read.**

Build and publish:

- A separate workflow `.github/workflows/bench-image.yml` holds the one job with
  cloud access. `ci.yml` stays no-cloud. The workflow authenticates to AWS via
  **GitHub OIDC assuming an IAM role** provisioned in the `bootstrap` stack, trust
  scoped to this repo and `main`, permissions scoped to pushing the bench-client
  ECR repo. No long-lived keys.
- Trigger is a **content-hash idempotent build**: on push to `main` and on
  `workflow_dispatch`, compute the image tag, check ECR, build and push only when
  the tag is absent. This covers a build on every image-affecting change, the
  first-ever build (tag absent), and manual runs, with one mechanism. No path
  filter to maintain.

Tag scheme (retires `latest`):

- `qwen3-8b-awq-<sha>` — immutable content identity. `<sha>` is the short hash of
  the last commit touching the image inputs: `bench/`, `src/slipstream_bench/`,
  `pyproject.toml`, `models.yaml`. The slug is the sanitized served-model id
  (lowercase, `/`→`-`), so the tag names the tokenizer the image carries.
- `qwen3-8b-awq-main` — floating pointer to the current `main` build; the
  like-for-like successor to what `latest` did on the pull side.
- The ECR repo stays `MUTABLE` (the floating `-main` tag needs it). A lifecycle
  rule keeps the last 10 `-sha` images and expires older ones; the existing
  untagged-7-day rule stays.

Model source of truth:

- A gpu-only `models.yaml` holds the served model, keyed to map one-to-one onto a
  future Helm chart's `values.yaml`: `model.hfId`, `model.revision`,
  `model.quantization`, `model.kvCacheDtype`. The bench build reads the
  `{hfId, revision}` subset via `yq` to bake the tokenizer and derive the slug;
  the justfile's duplicated `gpu_model` var is removed in favour of it.
- The serving manifest (`k8s/vllm-gpu.yaml`) does **not** read `models.yaml` yet.
  Rewiring it to a rendered config belongs with the deferred Argo CD / Helm work
  (spec §10, "L2a"), where `models.yaml` becomes chart values reconciled into the
  Deployment — not an interim envsubst or kustomize shim that would be discarded
  when the reconciler lands. Until then `models.yaml` is authoritative by
  convention, with a pointer comment in the manifest.

Consumers:

- `terraform/bench-endpoint`'s `bench_image_tag` defaults to the floating
  `qwen3-8b-awq-main`, overridable to a `-sha` tag for a reproducible run.
- `ci.yml` gains a build-only, no-push Dockerfile check on pull requests, so a
  broken Dockerfile fails the no-cloud gate without needing credentials.

## Consequences

- Nothing reaches ECR until a change lands on `main` (or a manual dispatch); pull
  requests validate the build but never publish.
- A tag now names its tokenizer and traces to the commit that produced it. A
  reproducible bench run pins the `-sha` tag; the routine dev loop follows
  `-main`, which still pulls with `imagePullPolicy: Always`.
- The served model has one definition. The bench image and the serving manifest
  still have to agree until L2a, but the pairing is documented in `models.yaml`
  and pointed at from the manifest, rather than silently duplicated in the
  justfile.
- The KB's production path for manifest value injection is Helm values under a
  GitOps reconciler (`helm-patterns.md`); Kustomize is sanctioned only as a
  post-render patch, and envsubst / Kustomize `replacements` are uncovered.
  Deferring the manifest rewire keeps this change on the CI-image concern and
  leaves the serving-plane migration to its own decision.
- Delivered as three pull requests, landed bottom-up: the `bootstrap` OIDC role
  and ECR lifecycle; `models.yaml` and the bench build/slug wiring; the
  `bench-image.yml` workflow that consumes both.
