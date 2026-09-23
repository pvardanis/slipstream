<!-- ADR recording why the justfile is split into six imported lifecycle files under just/, why `import` is used over `mod`, and the verbatim-move contract that the split preserves recipe behaviour. -->

# ADR-0013: Justfile split into imported lifecycle modules

- Status: Accepted
- Date: 2026-09-23

## Context

The root `justfile` had grown to ~800 lines spanning six lifecycle concerns —
cluster and node-pool provisioning, serving, benchmarking, observability, local
tests, and whole-stack orchestration. One flat file made a given concern's recipes
hard to locate and made unrelated edits collide.

`just` offers two ways to compose recipes across files:

- **`import "path"`** — textual inclusion. Every imported recipe joins the root's
  single flat namespace: `just gpu-deploy` runs the same whether it lives in the
  root or an imported file, and recipes call each other unqualified (`just bench`
  from inside `knob-sweep`).
- **`mod name "path"`** — a submodule with its own namespace. Recipes are invoked
  path-qualified (`just serve::gpu-deploy`) and cross-module calls need the prefix.

The recipes are not independent: they share the terraform-dir assignments
(`eks_dir`, `bootstrap_dir`, `bench_endpoint_dir`, the manifest and tool paths) and
cross-call constantly — `stack-up` chains `cluster-up gpu-pool-up gpu-deploy
bench-endpoint-up`, `bench` calls `_bench-image-ref`, `knob-sweep` calls
`gpu-deploy` and `bench`, `stack-down`/`cloud-verify` call the teardown recipes and
`_zero-leak-sweep`.

## Decision

**Split the recipe bodies verbatim into six files under `just/`**, imported from the
root:

- `cluster.just` — `_bootstrap-init`, `cluster-up`, `cluster-plan`, `gpu-pool-up`,
  `gpu-pool-down`, `cluster-down`, `bootstrap`.
- `serve.just` — the api-key helpers (`_ensure-api-key`, `_read-api-key`),
  `cpu-deploy`, `undeploy`, `_render-gpu-manifest`, `gpu-deploy`, `gpu-up`,
  `gpu-down`, `cpu-completion`, `gpu-completion`.
- `bench.just` — `_bench-image-ref`, `bench-image`, `bench`, `knob-sweep`,
  `bench-results-sync`, `prefix-cache`, `bench-endpoint-up`, `bench-endpoint-down`.
- `obs.just` — `obs-up`, `obs-down`, `obs-pivot`.
- `test.just` — `cli-test`, `obs-test`, `image-tag-test`, `build-image-test`.
- `orchestrate.just` — `stack-up`, `stack-down`, `cloud-verify`, `_zero-leak-sweep`.

The root `justfile` keeps the header comment, the shared assignments, the `default`
recipe, and the six `import` lines.

**`import` over `mod`.** The shared assignments and constant cross-calls want the
flat namespace `import` gives: no recipe body changes, no qualification, and the
terraform-dir vars stay visible to every file. `mod`'s namespacing would rename
every invocation and rot ~40 doc and comment references (`just gpu-deploy`,
`just bench`, `just stack-down`, …) across the repo for no gain — the split is about
locating recipes, not isolating them.

**Verbatim-move contract.** Recipe bodies moved byte-for-byte; no logic was edited.
Because `just --dump` (text) does not inline imported files — it prints the `import`
line and omits the imported bodies — the equivalence is proven against the JSON
dump, which flattens every imported recipe into one map with full bodies and no
source-file metadata: `just --dump --dump-format json` yields identical `recipes`,
`assignments`, `aliases`, and `settings` before and after the split, and
`just --summary` reports the same 37-recipe set.

## Consequences

- Each lifecycle concern lives in one file; unrelated edits no longer collide, and a
  recipe is found by its concern.
- Invocations, cross-calls, and the ~40 doc/comment references are unchanged: the
  flat namespace means `just <recipe>` behaves exactly as before.
- Equivalence after future edits is checked with `just --dump --dump-format json`
  (content) and `just --summary` (recipe set), not the text `just --dump`, which
  omits imported bodies under `import`.
- Adding a recipe means editing its concern's file, not the root; the root changes
  only when a new lifecycle concern (and its `import` line) is added.
