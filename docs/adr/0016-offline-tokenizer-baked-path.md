<!-- ADR recording why the bench client resolves its prompt-synthesis tokenizer from a snapshot baked into the image at a fixed path rather than pinning a Hugging Face revision at run time: `vllm bench serve` (the client) has no revision flag — it resolves a tokenizer only by name or local path — so the revision is pinned at build time by baking the model.yaml-pinned snapshot with save_pretrained to a stable directory, the image names that directory through an env var the CLI reads, and HF_HUB_OFFLINE forbids any run-time Hub call so a miss raises instead of silently fetching a different revision. Sharpens ADR-0012 §Amendment (the container runs one cell) and complements ADR-0006 (server-side --revision). Issue #142, PR #172. -->

# ADR-0016: The bench client's offline tokenizer is baked to a fixed path, not pinned by a runtime revision

- Status: Accepted
- Date: 2026-09-29

## Context

The bench client synthesises its request prompts against a tokenizer so the token
counts it reports are the server's own token counts. For that accounting to be exact,
the client's tokenizer must be the **same revision** the server loads: a different
snapshot of the same model can retokenize identical text to a different length, and the
TTFT/TPOT gates would then rest on token counts the server never saw.
[ADR-0006](0006-gpu-node-pool-and-awq-replica.md) pins the server's weights by
**HF revision commit SHA** (`vllm serve --revision <sha>`), and `model.yaml` is the
single source of truth for that revision (`hfId`, `revision`, `quantization`,
`kvCacheDtype`); it already feeds the deep config digest ([ADR-0012](0012-sweep-resumability-and-orchestrator-choice.md)).

[ADR-0012](0012-sweep-resumability-and-orchestrator-choice.md) §Amendment made the
container run **one cell** per `docker run` — `load-cell` fires a single
`vllm bench serve`. So the tokenizer question is a **client** question: how does
`vllm bench serve` get the pinned-revision tokenizer.

The asymmetry that shapes the decision: `vllm serve` (the server) accepts
`--revision`/`tokenizer_revision` and redirects a Hub load to a specific commit, but
`vllm bench serve` (the client) has **no such flag** — it resolves a tokenizer only by
**name or local path**. An earlier attempt to pass the pinned revision to the client
(`--tokenizer-revision <sha>`) was rejected at the client's argparse: the flag does not
exist there. Resolving the tokenizer by *name* alone would let the client's default
Hub load pick up `main`, a different revision than the server's pinned SHA.

## Decision

### Bake the tokenizer to a fixed path at build time, pinned to `model.yaml`'s revision

The image fetches the tokenizer once at build and writes it to a stable directory with
`save_pretrained`, pinned to the revision `just bench-image` reads from `model.yaml`:

```dockerfile
ARG MODEL=Qwen/Qwen2.5-0.5B-Instruct
ARG REVISION=main
RUN python3 -c "from transformers import AutoTokenizer; \
  AutoTokenizer.from_pretrained('${MODEL}', revision='${REVISION}').save_pretrained('/opt/hf/tokenizer')"
```

The revision is pinned **at build time**, where the client legitimately can pin it (the
build still reaches the Hub), rather than at run time, where the client has no flag to.
The build provenance *is* the revision pin: `model.yaml` → `--build-arg REVISION` → the
baked snapshot. `save_pretrained` to a **stable, named** directory (`/opt/hf/tokenizer`),
not the Hub cache's commit-SHA snapshot dir, so the run-time path is a constant the
client can be handed without reconstructing the SHA.

### The image names the path; the CLI reads it; the host runner carries nothing

The image declares where its tokenizer lives, and `load-cell` reads that into
`--tokenizer` through a Typer `envvar`:

```dockerfile
ENV SLIPSTREAM_BENCH_TOKENIZER_DIR=/opt/hf/tokenizer
```

```python
tokenizer: Annotated[str | None, typer.Option(envvar="SLIPSTREAM_BENCH_TOKENIZER_DIR", ...)] = None
```

So the image is the **single definition** of its own tokenizer path. The host runner
(`bench-sweep.sh`) and the orchestration layer pass no tokenizer of their own; they do
not know the path. Typer's precedence — explicit `--tokenizer` flag > env var > `None`
default — means an operator can still override the flag locally, and the commercial arm
can author a `tokenizer` in its config YAML, without the image env wiping either: the
CLI injects the env-derived value only when the flag is set, never overwriting a
config-authored tokenizer with nothing.

### `HF_HUB_OFFLINE` forbids any run-time Hub call

After the bake — the last step that needs the Hub — the image switches transformers to
offline:

```dockerfile
ENV HF_HUB_OFFLINE=1
```

This is not egress control; it is a behaviour switch that makes a cache/ref miss
**raise** instead of silently falling back to a Hub fetch of a different revision. It
turns the pinned-revision requirement into an enforced invariant: the baked snapshot is
the only tokenizer the client can resolve, so a run either uses the pinned revision or
fails loudly.

### Rejected alternatives

- **Pin the revision at run time (`--tokenizer-revision <sha>`).** The flag does not
  exist on `vllm bench serve`; this is the attempt that failed. The revision idiom is
  server-side only.
- **Point `--tokenizer` at the Hub cache's snapshot dir** (`$HF_HOME/hub/models--…/snapshots/<sha>`).
  This keeps the tokenizer where the default fetch already put it, but forces the
  run-time path to reconstruct the commit SHA and the cache's directory layout — brittle
  coupling to Hub cache internals. `save_pretrained` to a path we choose removes the SHA
  from every run-time surface.

## Consequences

- Re-pinning the model (a new `revision` in `model.yaml`) rolls the image content tag
  (`bench/image-tag.sh`), and the rebuild bakes the new snapshot to the same path. The
  digest already covers `model.yaml` (ADR-0012), so a re-pin invalidates the cell cache;
  nothing downstream of the path changes.
- `load-sweep` reads the same env var as `load-cell`. The container path is the
  single-cell `load-cell` (ADR-0012 §Amendment), so this is unused there, but keeping one
  option definition across both subcommands avoids a second, divergable spelling.
- The server-side `--revision` pin (ADR-0006) is unchanged: server and client pin the
  same revision through different idioms, because the two CLIs expose different ones.
- This ADR is prose only; the bake, the env var, and the CLI change ship in PR #172
  (issue #142).
