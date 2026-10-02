"""The config-provenance artifact: render a run's config for the Prefect run page.

A knob-sweep run's config is the deep digest's inputs (ADR-0012): model.yaml and
sweep-grid.yaml verbatim, and k8s/vllm-gpu.yaml — of which the serving image ref and the
vLLM container's args are shown. The args are embedded verbatim, placeholders and all
(``${MAX_NUM_SEQS}`` et al): the sweep-grid above decodes which values those placeholders
take per engine point, and the point slug on the Prefect graph names the point a redeploy
ran. ``render_config_artifact`` folds the version anchors — the bench-client image ref, the
orchestration image ref, the serving image ref, and the deep digest — and those config
bodies into one markdown body so an operator inspects the exact configuration a run executed
from the Prefect UI, without re-uploading the files anywhere. The render is a pure function
of its inputs, covered here without a Prefect server.
"""

from pathlib import Path

from slipstream_bench.orchestration.config_artifact import (
    ImageRefs,
    publish_config_artifact,
    render_config_artifact,
)
from slipstream_bench.orchestration.digest import DigestInputs

_GRID_YAML = """\
tier1:
  max_num_seqs: [16, 32]
  kv_cache_dtype: [fp8]
  prefix_caching:
    "on":
      flag: --enable-prefix-caching
      prefix_share: [10]
    "off":
      flag: --no-enable-prefix-caching
      prefix_share: [0]
tier2:
  max_concurrency: [8]
  burstiness: 1.0
load:
  total_len: 128
  num_prompts: 20
  num_prefixes: 2
  output_len: 16
  align_blocks: 0
  request_rate: 8
  seed: 0
  goodput: ["ttft:1000", "tpot:50"]
"""

_VLLM_ARGS_TEXT = """\
- --model
- Qwen/Qwen3-8B-AWQ
- --kv-cache-dtype
- ${KV_CACHE_DTYPE}
- --max-num-seqs
- ${MAX_NUM_SEQS}
- ${PREFIX_CACHING_FLAG}"""


def test_render_embeds_the_version_anchors_and_the_digest_inputs() -> None:
    markdown = render_config_artifact(
        run_id="e2e-smoke-5",
        bench_image_ref="repo/bench-client:qwen3-8b-awq-main",
        orch_image="repo/orchestration:abc123",
        digest="ab12cd34",
        model_yaml_text="model:\n  hfId: Qwen/Qwen3-8B\n",
        grid_text="tier1:\n  max_num_seqs: [16]\n",
        vllm_image_ref="vllm/vllm-openai:v0.29.0",
        vllm_args_text=_VLLM_ARGS_TEXT,
    )

    # The version anchors: the bench-client and orchestration image content-sha tags tie the
    # run to their builds, the digest proves config identity across runs.
    assert "e2e-smoke-5" in markdown
    assert "repo/bench-client:qwen3-8b-awq-main" in markdown
    assert "repo/orchestration:abc123" in markdown
    assert "ab12cd34" in markdown
    # model.yaml and the grid verbatim, so the operator reads the actual configuration.
    assert "hfId: Qwen/Qwen3-8B" in markdown
    assert "max_num_seqs: [16]" in markdown
    # The manifest's serving image ref and the vLLM args.
    assert "vllm/vllm-openai:v0.29.0" in markdown


def test_render_embeds_the_vllm_args_verbatim_with_the_placeholders_intact() -> None:
    markdown = render_config_artifact(
        run_id="run1",
        bench_image_ref="repo/bench-client:tag",
        orch_image="repo/orchestration:tag",
        digest="deadbeef",
        model_yaml_text="model: x\n",
        grid_text="tier1: {}\n",
        vllm_image_ref="vllm/vllm-openai:v0.29.0",
        vllm_args_text=_VLLM_ARGS_TEXT,
    )

    # The swept placeholders are shown as written, not resolved per point — the grid above
    # decodes the values each engine point redeploys with.
    assert "${MAX_NUM_SEQS}" in markdown
    assert "${KV_CACHE_DTYPE}" in markdown
    assert "${PREFIX_CACHING_FLAG}" in markdown
    assert "- --model" in markdown


def test_render_fences_the_three_config_bodies() -> None:
    markdown = render_config_artifact(
        run_id="run1",
        bench_image_ref="repo:tag",
        orch_image="repo/orchestration:tag",
        digest="deadbeef",
        model_yaml_text="model: x\n",
        grid_text="tier1: {}\n",
        vllm_image_ref="vllm/vllm-openai:v0.29.0",
        vllm_args_text=_VLLM_ARGS_TEXT,
    )

    # model.yaml, sweep-grid.yaml, and the vLLM args — one fence each.
    assert markdown.count("```yaml") == 3


def test_render_orders_the_header_bullets_before_the_fenced_bodies() -> None:
    markdown = render_config_artifact(
        run_id="run1",
        bench_image_ref="repo/bench-client:tag",
        orch_image="repo/orchestration:tag",
        digest="deadbeef",
        model_yaml_text="model: x\n",
        grid_text="tier1: {}\n",
        vllm_image_ref="vllm/vllm-openai:v0.29.0",
        vllm_args_text=_VLLM_ARGS_TEXT,
    )

    # The four anchors are labelled bullet lines, not bare values floating in the body, so a
    # dropped label or a reordered section is caught, not silently accepted.
    assert "- bench_image: `repo/bench-client:tag`" in markdown
    assert "- orch_image: `repo/orchestration:tag`" in markdown
    assert "- vllm_image: `vllm/vllm-openai:v0.29.0`" in markdown
    assert "- digest: `deadbeef`" in markdown
    # Header and its bullets precede the fenced bodies; the vLLM args come last.
    assert markdown.index("## knob-sweep config") < markdown.index("- bench_image:")
    assert markdown.index("- digest:") < markdown.index("```yaml")
    assert markdown.index("### sweep-grid.yaml") < markdown.index("### vllm args")


def test_render_strips_trailing_whitespace_inside_each_fence() -> None:
    markdown = render_config_artifact(
        run_id="run1",
        bench_image_ref="repo:tag",
        orch_image="repo/orchestration:tag",
        digest="deadbeef",
        model_yaml_text="model: x\n\n\n",
        grid_text="tier1: {}\n",
        vllm_image_ref="vllm/vllm-openai:v0.29.0",
        vllm_args_text=_VLLM_ARGS_TEXT,
    )

    # A body's trailing blank lines are stripped so the closing fence sits clean against the
    # content, never after a run of empty lines.
    assert "model: x\n```" in markdown


def test_publish_reads_the_config_and_keys_the_artifact(tmp_path: Path) -> None:
    model_yaml = tmp_path / "model.yaml"
    sweep_grid = tmp_path / "sweep-grid.yaml"
    vllm_manifest = tmp_path / "vllm-gpu.yaml"
    model_yaml.write_text("model:\n  hfId: Qwen/Qwen3-8B\n", encoding="utf-8")
    sweep_grid.write_text(_GRID_YAML, encoding="utf-8")
    vllm_manifest.write_text(
        "kind: Deployment\n"
        "spec:\n  template:\n    spec:\n      containers:\n"
        "        - name: vllm\n"
        "          image: vllm/vllm-openai:v0.29.0\n"
        "          args:\n"
        "            - --max-num-seqs\n"
        '            - "${MAX_NUM_SEQS}"\n'
        "            - ${PREFIX_CACHING_FLAG}\n",
        encoding="utf-8",
    )
    digest_inputs = DigestInputs(
        model_yaml=model_yaml, sweep_grid=sweep_grid, vllm_manifest=vllm_manifest
    )
    captured: dict[str, object] = {}

    publish_config_artifact(
        run_id="e2e-smoke-5",
        images=ImageRefs(
            bench="repo/bench-client:tag", orch="repo/orchestration:abc123"
        ),
        digest_inputs=digest_inputs,
        publish=lambda **kwargs: captured.update(kwargs),
    )

    # Keyed so Prefect keeps a cross-run history of the artifact.
    assert captured["key"] == "knob-sweep-config"
    markdown = captured["markdown"]
    assert isinstance(markdown, str)
    # model.yaml and the grid were read off disk; the serving image ref and the vLLM args
    # (verbatim, placeholders intact) were read out of the manifest and folded in.
    assert "hfId: Qwen/Qwen3-8B" in markdown
    assert "vllm/vllm-openai:v0.29.0" in markdown
    assert "--max-num-seqs" in markdown
    assert "${MAX_NUM_SEQS}" in markdown
    assert "${PREFIX_CACHING_FLAG}" in markdown
    assert "repo/orchestration:abc123" in markdown
    # The deep digest is computed from the inputs, not passed in, so the artifact shows the
    # same key resume computes.
    assert digest_inputs.digest() in markdown
