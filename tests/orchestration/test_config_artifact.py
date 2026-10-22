"""The config-provenance artifact: render a run's config for the Prefect run page.

A knob-sweep run's config is model.yaml and sweep-grid.yaml verbatim, and k8s/vllm-gpu.yaml — of which the serving image ref and the
vLLM container's args are shown. The args are embedded verbatim, placeholders and all
(``${MAX_NUM_SEQS}`` et al): the sweep-grid above decodes which values those placeholders
take per engine point, and the point slug on the Prefect graph names the point a redeploy
ran. ``render_config_artifact`` folds the version anchors — the bench-client image ref, the
orchestration image ref, and the serving image ref — and those config
bodies into one markdown body so an operator inspects the exact configuration a run executed
from the Prefect UI, without re-uploading the files anywhere. The render is a pure function
of its inputs, covered here without a Prefect server.
"""

import logging
from pathlib import Path

import pytest

from slipstream_bench.orchestration.config_artifact import (
    ImageRefs,
    publish_config_artifact,
    read_orch_image_ref,
    render_config_artifact,
)

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


def test_render_embeds_the_version_anchors_and_the_config_bodies() -> None:
    markdown = render_config_artifact(
        flow="knob-sweep",
        run_id="e2e-smoke-5",
        bench_image_ref="repo/bench-client:qwen3-8b-awq-main",
        orch_image="repo/orchestration:abc123",
        model_yaml_text="model:\n  hfId: Qwen/Qwen3-8B\n",
        workload_name="sweep-grid.yaml",
        workload_text="tier1:\n  max_num_seqs: [16]\n",
        vllm_image_ref="vllm/vllm-openai:v0.29.0",
        vllm_args_text=_VLLM_ARGS_TEXT,
    )

    # The version anchors: the bench-client and orchestration image content-sha tags tie the
    # run to their builds.
    assert "e2e-smoke-5" in markdown
    assert "repo/bench-client:qwen3-8b-awq-main" in markdown
    assert "repo/orchestration:abc123" in markdown
    # model.yaml and the grid verbatim, so the operator reads the actual configuration.
    assert "hfId: Qwen/Qwen3-8B" in markdown
    assert "max_num_seqs: [16]" in markdown
    # The manifest's serving image ref and the vLLM args.
    assert "vllm/vllm-openai:v0.29.0" in markdown


def test_render_embeds_the_vllm_args_verbatim_with_the_placeholders_intact() -> None:
    markdown = render_config_artifact(
        flow="knob-sweep",
        run_id="run1",
        bench_image_ref="repo/bench-client:tag",
        orch_image="repo/orchestration:tag",
        model_yaml_text="model: x\n",
        workload_name="sweep-grid.yaml",
        workload_text="tier1: {}\n",
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
        flow="knob-sweep",
        run_id="run1",
        bench_image_ref="repo:tag",
        orch_image="repo/orchestration:tag",
        model_yaml_text="model: x\n",
        workload_name="sweep-grid.yaml",
        workload_text="tier1: {}\n",
        vllm_image_ref="vllm/vllm-openai:v0.29.0",
        vllm_args_text=_VLLM_ARGS_TEXT,
    )

    # model.yaml, sweep-grid.yaml, and the vLLM args — one fence each.
    assert markdown.count("```yaml") == 3


def test_render_orders_the_header_bullets_before_the_fenced_bodies() -> None:
    markdown = render_config_artifact(
        flow="knob-sweep",
        run_id="run1",
        bench_image_ref="repo/bench-client:tag",
        orch_image="repo/orchestration:tag",
        model_yaml_text="model: x\n",
        workload_name="sweep-grid.yaml",
        workload_text="tier1: {}\n",
        vllm_image_ref="vllm/vllm-openai:v0.29.0",
        vllm_args_text=_VLLM_ARGS_TEXT,
    )

    # The three anchors are labelled bullet lines, not bare values floating in the body, so a
    # dropped label or a reordered section is caught, not silently accepted.
    assert "- bench_image: `repo/bench-client:tag`" in markdown
    assert "- orch_image: `repo/orchestration:tag`" in markdown
    assert "- vllm_image: `vllm/vllm-openai:v0.29.0`" in markdown
    # Header and its bullets precede the fenced bodies; the vLLM args come last.
    assert markdown.index("## knob-sweep config") < markdown.index("- bench_image:")
    assert markdown.index("- vllm_image:") < markdown.index("```yaml")
    assert markdown.index("### sweep-grid.yaml") < markdown.index("### vllm args")


def test_render_strips_trailing_whitespace_inside_each_fence() -> None:
    markdown = render_config_artifact(
        flow="knob-sweep",
        run_id="run1",
        bench_image_ref="repo:tag",
        orch_image="repo/orchestration:tag",
        model_yaml_text="model: x\n\n\n",
        workload_name="sweep-grid.yaml",
        workload_text="tier1: {}\n",
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
    captured: dict[str, object] = {}

    publish_config_artifact(
        flow="knob-sweep",
        run_id="e2e-smoke-5",
        images=ImageRefs(
            bench="repo/bench-client:tag", orch="repo/orchestration:abc123"
        ),
        model_yaml=model_yaml,
        workload=sweep_grid,
        vllm_manifest=vllm_manifest,
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


def test_publish_keys_and_heads_the_artifact_by_flow_and_names_the_workload_file(
    tmp_path: Path,
) -> None:
    model_yaml = tmp_path / "model.yaml"
    workload = tmp_path / "kv-top.yaml"
    vllm_manifest = tmp_path / "vllm-gpu.yaml"
    model_yaml.write_text("model:\n  hfId: Qwen/Qwen3-8B\n", encoding="utf-8")
    workload.write_text("runs_per_arm: 2\n", encoding="utf-8")
    vllm_manifest.write_text(
        "kind: Deployment\n"
        "spec:\n  template:\n    spec:\n      containers:\n"
        "        - name: vllm\n"
        "          image: vllm/vllm-openai:v0.29.0\n"
        "          args:\n"
        "            - --kv-transfer-config\n"
        "            - '${KV_TRANSFER_CONFIG}'\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    publish_config_artifact(
        flow="kv-ab",
        run_id="ab1",
        images=ImageRefs(bench="repo/bench-client:tag", orch="repo/orchestration:qa"),
        model_yaml=model_yaml,
        workload=workload,
        vllm_manifest=vllm_manifest,
        publish=lambda **kwargs: captured.update(kwargs),
    )

    # Each flow keeps its own cross-run artifact history, and the workload section is
    # titled with the file the run was given.
    assert captured["key"] == "kv-ab-config"
    markdown = captured["markdown"]
    assert isinstance(markdown, str)
    assert markdown.startswith("## kv-ab config (ab1)\n")
    assert "### kv-top.yaml\n\n```yaml\nruns_per_arm: 2\n```" in markdown
    assert "${KV_TRANSFER_CONFIG}" in markdown


def test_read_orch_image_ref_returns_the_baked_ref_without_logging(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ORCH_IMAGE_REF", "repo/orchestration:sha256-abc")

    with caplog.at_level(logging.INFO):
        ref = read_orch_image_ref(logging.getLogger("run"))

    assert ref == "repo/orchestration:sha256-abc"
    assert caplog.records == []


@pytest.mark.parametrize("baked", [None, ""])
def test_read_orch_image_ref_logs_and_reads_unknown_when_no_ref_is_baked(
    baked: str | None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    if baked is None:
        monkeypatch.delenv("ORCH_IMAGE_REF", raising=False)
    else:
        monkeypatch.setenv("ORCH_IMAGE_REF", baked)

    with caplog.at_level(logging.INFO):
        ref = read_orch_image_ref(logging.getLogger("run"))

    # A build that baked no ref reads as "unknown", and says so on the run's log.
    assert ref == "unknown"
    assert [record.getMessage() for record in caplog.records] == [
        'ORCH_IMAGE_REF unset; recording the orchestration image as "unknown"'
    ]
