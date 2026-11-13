"""The cold-start-breakdown run: its cycle under the rollout freeze, its record and artifact.

:func:`record_cold_start` drives the cycle inside the run's rollout freeze (ADR-0029), so
Argo CD rolls nothing while the replica cycles, then writes the run's breakdown under the
results bucket's ``cold-start`` root and publishes it to the run page. These tests drive
it over the production collaborators with only ``Kubectl`` faked, the freeze recording
its acquire and release among the cluster's steps.
"""

import json
import logging
from typing import Any

import pytest

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

    def record(self) -> str:
        clock = FakeClock()
        return record_cold_start(
            run_id="cs-1",
            bucket="bench",
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


def test_the_breakdown_is_recorded_under_the_cold_start_root() -> None:
    run = _Run(CyclingCluster())

    pointer = run.record()

    assert pointer == "s3://bench/cold-start/cs-1/r1.json"
    [put] = run.s3.puts
    assert (put["Bucket"], put["Key"], put["ContentType"]) == (
        "bench",
        "cold-start/cs-1/r1.json",
        "application/json",
    )
    assert json.loads(put["Body"]) == {
        "run": 1,
        "total_s": 545.0,
        "capacity_wait_s": 3.0,
        "node_launch_s": 47.0,
        "image_pull_s": 264.0,
        "unattributed_s": 231.0,
    }


def test_the_breakdown_is_published_to_the_run_page() -> None:
    run = _Run(CyclingCluster())

    run.record()

    assert run.artifacts == [
        {
            "key": "cold-start-breakdown",
            "markdown": (
                "## Cold-start breakdown\n"
                "\n"
                "Run 1 of cs-1, pod created → Ready (ADR-0030); recorded at "
                "s3://bench/cold-start/cs-1/r1.json.\n"
                "\n"
                "| Phase | Seconds |\n"
                "|---|---:|\n"
                "| Capacity wait | 3.0 |\n"
                "| Node launch | 47.0 |\n"
                "| Image pull | 264.0 |\n"
                "| Unattributed | 231.0 |\n"
                "| **Total** | **545.0** |\n"
            ),
        }
    ]


def test_a_failed_cycle_releases_the_freeze_and_records_nothing() -> None:
    run = _Run(CyclingCluster(ready=False))

    with pytest.raises(ColdStartMarkerError, match="Ready"):
        run.record()

    assert run.cluster.steps[-2:] == ["scale 0", "freeze release"]
    assert run.s3.puts == []
    assert run.artifacts == []


def test_a_failed_write_leaves_the_breakdown_in_the_run_s_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = _Run(CyclingCluster())
    run.s3.fails = True
    caplog.set_level(logging.INFO)

    with pytest.raises(OSError, match="AccessDenied"):
        run.record()

    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "slipstream_bench.orchestration.flows.cold_start"
    ] == [
        (
            logging.INFO,
            (
                "cold-start run=cs-1 measured {'run': 1, 'total_s': 545.0, "
                "'capacity_wait_s': 3.0, 'node_launch_s': 47.0, 'image_pull_s': "
                "264.0, 'unattributed_s': 231.0}; writing it to "
                "s3://bench/cold-start/cs-1/r1.json"
            ),
        )
    ]
