"""Per-cell wiring: render a cell config, address its S3 object, drive it over SSM.

ADR-0012 §Amendment: each cell is one ``docker run`` on the bench host over SSM. The
flow renders a ``CellConfig`` to the YAML ``load-cell`` reads (the CLI-injected keys
excluded, so ``load_cell_config`` re-injecting them round-trips), addresses the cell's
result object under ``sweeps/<run_id>/<basename>``, and builds the ``execute_func`` the
cell task runs: send ``bench-sweep.sh`` over SSM, then download the result JSON the
validity gate reads. The transport and S3 clients are faked so the wiring is covered
without AWS.
"""

import base64
from pathlib import Path

import pytest

from slipstream_bench.orchestration.cell_run import (
    CellExecutionContext,
    CellResultError,
    build_cell_command,
    build_cell_execution,
    get_cell_result_uri,
    get_cell_s3_key,
    render_cell_config,
)
from slipstream_bench.sweep.config import load_cell_config, load_sweep_config
from slipstream_bench.sweep.runner import get_cell_basename


def _first_cell():
    sweep = load_sweep_config(
        Path("bench/load-sweep.yaml"),
        base_url="http://ignored:8000",
        model="Qwen/Qwen2.5-0.5B-Instruct",
        out_dir="bench/results",
        commercial=False,
    )
    return next(sweep.cells())


def test_render_cell_config_round_trips_through_load_cell_config(
    tmp_path: Path,
) -> None:
    cell = _first_cell()
    rendered = tmp_path / "cell-config.yaml"
    rendered.write_text(render_cell_config(cell), encoding="utf-8")

    reloaded = load_cell_config(
        rendered,
        base_url="http://127.0.0.1:9000",
        model="Qwen/Qwen2.5-0.5B-Instruct",
        out_dir="/out",
        commercial=False,
    )

    assert reloaded.prefix_share == cell.prefix_share
    assert reloaded.burstiness == cell.burstiness
    assert reloaded.max_concurrency == cell.max_concurrency
    assert reloaded.num_prompts == cell.num_prompts


def test_render_cell_config_omits_the_cli_injected_keys() -> None:
    rendered = render_cell_config(_first_cell())

    for key in ("base_url", "model", "revision", "out_dir", "commercial"):
        assert f"{key}:" not in rendered


def test_cell_result_uri_addresses_the_run_prefix() -> None:
    cell = _first_cell()

    uri = get_cell_result_uri("bench-bucket", "20260101T000000Z/mns64", cell)

    assert uri == (
        f"s3://bench-bucket/sweeps/20260101T000000Z/mns64/{get_cell_basename(cell)}"
    )
    assert get_cell_s3_key("20260101T000000Z/mns64", cell) == (
        f"sweeps/20260101T000000Z/mns64/{get_cell_basename(cell)}"
    )


def test_build_cell_command_prefixes_the_host_environment() -> None:
    command = build_cell_command(
        image_ref="repo:tag",
        bucket="bench-bucket",
        model="Qwen/Qwen2.5-0.5B-Instruct",
        run_id="run1/mns64",
        cell_config_b64="Y2ZnCg==",
    )

    assert command.endswith("/usr/local/bin/bench-sweep.sh")
    assert "IMAGE_REF='repo:tag'" in command
    assert "RESULTS_BUCKET='bench-bucket'" in command
    assert "MODEL='Qwen/Qwen2.5-0.5B-Instruct'" in command
    assert "RUN_ID='run1/mns64'" in command
    assert "CELL_CONFIG_B64='Y2ZnCg=='" in command
    assert "SWEEP_ARGS_B64" not in command
    assert "REVISION" not in command


def test_build_cell_command_appends_optional_args() -> None:
    command = build_cell_command(
        image_ref="repo:tag",
        bucket="b",
        model="m",
        run_id="r",
        cell_config_b64="Y2ZnCg==",
        sweep_args_b64="LS1kcnktcnVu",
    )

    assert "SWEEP_ARGS_B64='LS1kcnktcnVu'" in command


def test_build_cell_command_prefixes_the_pinned_revision() -> None:
    command = build_cell_command(
        image_ref="repo:tag",
        bucket="b",
        model="m",
        run_id="r",
        cell_config_b64="Y2ZnCg==",
        revision="4da05a8edb55c6046cce958586c33b61da07bb79",
    )

    assert "REVISION='4da05a8edb55c6046cce958586c33b61da07bb79'" in command


class _FakeSsm:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def send_command(self, **kwargs: object) -> dict[str, object]:
        params = kwargs["Parameters"]
        assert isinstance(params, dict)
        self.commands.append(params["commands"][0])
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, **_kwargs: object) -> dict[str, object]:
        return {"Status": "Success", "StandardErrorContent": ""}


class _FakeS3:
    def __init__(self) -> None:
        self.downloads: list[tuple[str, str]] = []

    def download_file(self, bucket: str, key: str, dest: str) -> None:
        self.downloads.append((bucket, key))
        Path(dest).write_text('{"completed": 1}', encoding="utf-8")


def test_build_cell_execution_runs_then_downloads(tmp_path: Path) -> None:
    cell = _first_cell()
    ssm = _FakeSsm()
    s3 = _FakeS3()
    dest = tmp_path / "cell.json"

    execute = build_cell_execution(
        cell,
        context=CellExecutionContext(
            ssm_client=ssm,
            s3_client=s3,
            instance_id="i-1",
            image_ref="repo:tag",
            bucket="bench-bucket",
            model="Qwen/Qwen2.5-0.5B-Instruct",
            run_id="run1/mns64",
            poll_interval_s=0.0,
            sleep=lambda _s: None,
        ),
        dest=dest,
    )
    execute()

    assert len(ssm.commands) == 1
    sent = ssm.commands[0]
    assert "/usr/local/bin/bench-sweep.sh" in sent
    decoded = base64.b64decode(
        sent.split("CELL_CONFIG_B64='")[1].split("'")[0]
    ).decode()
    assert f"prefix_share: {cell.prefix_share}" in decoded
    assert s3.downloads == [("bench-bucket", get_cell_s3_key("run1/mns64", cell))]
    assert dest.read_text(encoding="utf-8") == '{"completed": 1}'


class _DownloadFails:
    def download_file(self, _bucket: str, _key: str, _dest: str) -> None:
        raise RuntimeError("404 Not Found")


def test_build_cell_execution_wraps_a_missing_result(tmp_path: Path) -> None:
    execute = build_cell_execution(
        _first_cell(),
        context=CellExecutionContext(
            ssm_client=_FakeSsm(),
            s3_client=_DownloadFails(),
            instance_id="i-1",
            image_ref="repo:tag",
            bucket="bench-bucket",
            model="Qwen/Qwen2.5-0.5B-Instruct",
            run_id="run1/mns64",
            poll_interval_s=0.0,
            sleep=lambda _s: None,
        ),
        dest=tmp_path / "cell.json",
    )

    with pytest.raises(CellResultError, match="could not be downloaded"):
        execute()
