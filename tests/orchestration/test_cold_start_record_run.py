"""The cold-start-breakdown run: its cycles under the rollout freeze, records, artifact.

:func:`record_cold_start` drives the run's cycles inside its rollout freeze (ADR-0029), so
Argo CD rolls nothing while the replica cycles, each from a verified zero, then writes
each cycle's breakdown under the results bucket's ``cold-start`` root, publishes the
phases' spread to the run page, and uploads the stacked-bar chart. These tests drive it
over the production collaborators with only ``Kubectl`` faked, the freeze recording its
acquire and release among the cluster's steps.
"""

import json
import logging
from typing import Any

import pytest

from slipstream_bench.orchestration.cluster import KubectlError
from slipstream_bench.orchestration.cold_start_cluster import ColdStartMarkerError
from slipstream_bench.orchestration.flows.cold_start import (
    build_cold_start_collaborators,
    record_cold_start,
)
from slipstream_bench.orchestration.rollout_freeze import hold_rollout_freeze
from tests.orchestration.cold_start_fakes import CyclingCluster, FakeClock
from tests.orchestration.rollout_freeze_fakes import RecordingFreeze


class _RecordingS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.fails = False

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        if self.fails:
            raise OSError("S3 PutObject: AccessDenied")
        self.puts.append(kwargs)
        return {}


class _Run:
    """One run's fakes: the cluster and the freeze sharing one step log."""

    def __init__(self, cluster: CyclingCluster) -> None:
        self.cluster = cluster
        self.freeze = RecordingFreeze(cluster.steps)
        self.s3 = _RecordingS3()
        self.artifacts: list[dict[str, Any]] = []

    def record(self, runs: int = 1) -> str:
        clock = FakeClock()
        return record_cold_start(
            run_id="cs-1",
            bucket="bench",
            runs=runs,
            collaborators=build_cold_start_collaborators(
                self.cluster, sleep=clock.sleep, now=clock.now
            ),
            freeze=hold_rollout_freeze(self.freeze, holder="cs-1", duration_s=3600),
            s3_client=self.s3,
            publish_artifact=lambda **kwargs: self.artifacts.append(kwargs),
        )


def test_the_cycle_runs_inside_the_run_s_rollout_freeze() -> None:
    run = _Run(CyclingCluster())

    run.record()

    assert run.freeze.calls == [("acquire", "cs-1", "3600"), ("release", "cs-1")]
    assert run.cluster.steps[0] == "freeze acquire"
    assert run.cluster.steps[1] == "scale 0"
    assert run.cluster.steps[-2:] == ["scale 0", "freeze release"]


_RECORDED = {
    "total_s": 545.0,
    "capacity_wait_s": 3.0,
    "node_launch_s": 47.0,
    "image_pull_s": 264.0,
    "weight_download_s": 42.213,
    "engine_init_s": 104.27,
    "readiness_lag_s": 7.0,
    "unattributed_s": 77.517,
    "torch_compile_s": 44.91,
    "graph_capture_s": 4.0,
    "profiling_run_s": 0.1,
}


def test_each_run_cycles_the_replica_from_a_verified_zero() -> None:
    run = _Run(CyclingCluster())

    run.record(runs=3)

    scale_ups = [i for i, step in enumerate(run.cluster.steps) if step == "scale 1"]
    assert len(scale_ups) == 3
    # Every scale-up follows a scale to zero, then only NodeClaim polls, the last of which
    # found no GPU NodeClaim left; the polls before it are the warm node draining.
    for start, scale_up in zip([0, *scale_ups], scale_ups, strict=False):
        cycle = run.cluster.steps[start:scale_up]
        last_scale_zero = len(cycle) - 1 - cycle[::-1].index("scale 0")
        polls = cycle[last_scale_zero + 1 :]
        assert polls
        assert set(polls) == {"poll gpu nodeclaims"}


def test_each_run_s_breakdown_is_recorded_under_the_cold_start_root() -> None:
    run = _Run(CyclingCluster())

    pointer = run.record(runs=2)

    assert pointer == "s3://bench/cold-start/cs-1/"
    records = [put for put in run.s3.puts if put["ContentType"] == "application/json"]
    assert [(put["Bucket"], put["Key"]) for put in records] == [
        ("bench", "cold-start/cs-1/r1.json"),
        ("bench", "cold-start/cs-1/r2.json"),
    ]
    assert [json.loads(put["Body"]) for put in records] == [
        {"run": 1, **_RECORDED},
        {"run": 2, **_RECORDED},
    ]


def test_the_phases_spread_is_published_to_the_run_page() -> None:
    run = _Run(CyclingCluster())

    run.record(runs=2)

    assert run.artifacts == [
        {
            "key": "cold-start-breakdown",
            "markdown": (
                "## Cold-start breakdown\n"
                "\n"
                "2 runs of cs-1, pod created → Ready (ADR-0030): each phase's median "
                "and, bracketed, its range across the runs; recorded under "
                "s3://bench/cold-start/cs-1/.\n"
                "\n"
                "| phase | seconds |\n"
                "| --- | --- |\n"
                "| Capacity wait | 3.0 [3.0–3.0] |\n"
                "| Node launch | 47.0 [47.0–47.0] |\n"
                "| Image pull | 264.0 [264.0–264.0] |\n"
                "| Weight download | 42.2 [42.2–42.2] |\n"
                "| Engine init | 104.3 [104.3–104.3] |\n"
                "| ↳ torch.compile | 44.9 [44.9–44.9] |\n"
                "| ↳ Graph capture | 4.0 [4.0–4.0] |\n"
                "| ↳ Profiling run | 0.1 [0.1–0.1] |\n"
                "| Readiness lag | 7.0 [7.0–7.0] |\n"
                "| Unattributed | 77.5 [77.5–77.5] |\n"
                "| **Total** | **545.0 [545.0–545.0]** |\n"
            ),
        }
    ]


def test_the_stacked_bar_chart_is_uploaded_under_the_run_s_charts_prefix() -> None:
    run = _Run(CyclingCluster())

    run.record(runs=2)

    [chart] = [put for put in run.s3.puts if put["ContentType"] == "image/png"]
    assert (chart["Bucket"], chart["Key"]) == (
        "bench",
        "cold-start/cs-1/charts/cold-start-breakdown.png",
    )
    assert chart["Body"].startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize("runs", [0, 6])
def test_a_run_count_outside_one_to_five_is_refused_before_the_freeze(
    runs: int,
) -> None:
    run = _Run(CyclingCluster())

    with pytest.raises(ValueError, match=f"run count {runs} is not between 1 and 5"):
        run.record(runs=runs)

    assert run.freeze.calls == []
    assert run.cluster.steps == []


def test_a_failed_cycle_releases_the_freeze_and_records_nothing() -> None:
    run = _Run(CyclingCluster(ready=False))

    with pytest.raises(ColdStartMarkerError, match="Ready"):
        run.record()

    assert run.cluster.steps[-2:] == ["scale 0", "freeze release"]
    assert run.s3.puts == []
    assert run.artifacts == []


def test_a_run_failing_partway_leaves_the_runs_before_it_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The second run's scale-up (the fifth patch) fails: the first run is logged, and
    # nothing is recorded, since a failed flow is re-run whole.
    run = _Run(CyclingCluster(failing_patches={4}))
    caplog.set_level(logging.INFO)

    with pytest.raises(KubectlError, match="server is unavailable"):
        run.record(runs=3)

    assert _measured(caplog) == [f"cold-start run=cs-1 r1 measured {_logged(1)}"]
    assert run.cluster.steps[-2:] == ["scale 0", "freeze release"]
    assert run.s3.puts == []
    assert run.artifacts == []


def test_a_run_whose_chart_cannot_be_drawn_records_nothing() -> None:
    # torch.compile logged longer than the whole of engine init: a misread marker the
    # chart refuses, found before any record or the artifact is written.
    cluster = CyclingCluster()
    cluster.engine_log = cluster.engine_log.replace(
        "torch.compile took 44.91 s", "torch.compile took 200.00 s"
    )
    run = _Run(cluster)

    with pytest.raises(ValueError, match="exceeds its engine init"):
        run.record()

    assert run.s3.puts == []
    assert run.artifacts == []


def test_five_runs_are_measured_and_recorded() -> None:
    run = _Run(CyclingCluster())

    run.record(runs=5)

    assert [put["Key"] for put in run.s3.puts if put["Key"].endswith(".json")] == [
        f"cold-start/cs-1/r{n}.json" for n in range(1, 6)
    ]


def test_a_failed_write_leaves_every_breakdown_in_the_run_s_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = _Run(CyclingCluster())
    run.s3.fails = True
    caplog.set_level(logging.INFO)

    with pytest.raises(OSError, match="AccessDenied"):
        run.record(runs=2)

    assert _measured(caplog) == [
        f"cold-start run=cs-1 r1 measured {_logged(1)}",
        f"cold-start run=cs-1 r2 measured {_logged(2)}",
    ]


def _logged(run: int) -> dict[str, float]:
    """The breakdown record as a measured line logs it."""
    return {"run": run, **_RECORDED}


def _measured(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The flow's lines logging a measured breakdown, in order."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "slipstream_bench.orchestration.flows.cold_start"
        and " measured " in record.getMessage()
    ]
