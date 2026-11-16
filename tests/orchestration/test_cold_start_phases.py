"""The cold start's phases, read through the ``Kubectl`` seam.

:func:`read_cold_start_breakdown` reads the replica's pod, the namespace's events, the
hosting NodeClaim and the engine's log, and breaks the pod-created → Ready total into
capacity wait, node launch, image pull, weight download, engine init, readiness lag and
the unattributed remainder, with engine init's torch.compile, graph capture and
profiling-run detail (ADR-0030). These tests replay the one recorded true-zero cold start
(tests/fixtures/cold-start) through a ``Kubectl`` fake; the expected seconds are the
hand-worked breakdown on #37, with readiness lag read from the server's start to the
second, the resolution of the pod's Ready condition.
"""

import re
from copy import deepcopy

import pytest

from slipstream_bench.contract.cold_start_record import ColdStartRecordError
from slipstream_bench.orchestration.cold_start_cluster import (
    ColdStartCycleError,
    ColdStartMarkerError,
    read_cold_start_breakdown,
)
from tests.orchestration.cold_start_fakes import RecordedCluster


def test_the_recorded_cold_start_breaks_down_into_its_six_phases() -> None:
    breakdown = read_cold_start_breakdown(RecordedCluster(), run=1)

    assert breakdown.run == 1
    assert breakdown.total_s == 545.0
    assert breakdown.capacity_wait_s == 3.0
    assert breakdown.node_launch_s == 47.0
    assert breakdown.image_pull_s == 264.0
    assert breakdown.weight_download_s == 42.213
    assert breakdown.engine_init_s == 104.27
    assert breakdown.readiness_lag_s == 7.0
    assert breakdown.unattributed_s == 77.517


def test_engine_init_detail_is_read_and_both_graph_captures_are_summed() -> None:
    breakdown = read_cold_start_breakdown(RecordedCluster(), run=1)

    assert breakdown.torch_compile_s == 44.91
    assert breakdown.graph_capture_s == 4.0
    assert breakdown.profiling_run_s == 0.1


def test_summed_graph_captures_are_kept_to_the_millisecond() -> None:
    cluster = RecordedCluster()
    captures = iter(["1.1", "2.2"])
    cluster.engine_log = "\n".join(
        line.replace("finished in 2 secs", f"finished in {next(captures)} secs")
        if "Graph capturing finished in" in line
        else line
        for line in cluster.engine_log.splitlines()
    )

    assert read_cold_start_breakdown(cluster, run=1).graph_capture_s == 3.3


def test_the_engine_log_is_read_with_the_runtime_s_timestamps() -> None:
    cluster = RecordedCluster()

    read_cold_start_breakdown(cluster, run=1)

    assert [
        "-n",
        "slipstream",
        "logs",
        "vllm-gpu-bcd494f9-xsjvl",
        "-c",
        "vllm",
        "--timestamps",
    ] in cluster.calls


def test_the_record_carries_every_phase_and_they_add_up_to_the_total() -> None:
    record = read_cold_start_breakdown(RecordedCluster(), run=2).to_record()

    assert record == {
        "run": 2,
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


def test_another_pods_events_do_not_move_the_image_pull() -> None:
    cluster = RecordedCluster()
    pulled = next(e for e in cluster.events["items"] if e["reason"] == "Pulled")
    other = deepcopy(pulled)
    other["involvedObject"]["name"] = "vllm-gpu-old-pod"
    other["firstTimestamp"] = other["lastTimestamp"] = "2026-11-13T15:23:00Z"
    cluster.events["items"].insert(0, other)

    assert read_cold_start_breakdown(cluster, run=1).image_pull_s == 264.0


@pytest.mark.parametrize(
    ("field", "value"),
    [("fieldPath", "spec.initContainers{model-fetch}"), ("kind", "ReplicaSet")],
)
def test_an_earlier_pull_by_another_container_or_object_does_not_move_the_image_pull(
    field: str, value: str
) -> None:
    cluster = RecordedCluster()
    pulling = next(e for e in cluster.events["items"] if e["reason"] == "Pulling")
    other = deepcopy(pulling)
    other["involvedObject"][field] = value
    other["firstTimestamp"] = other["lastTimestamp"] = "2026-11-13T15:22:00Z"
    cluster.events["items"].insert(0, other)

    assert read_cold_start_breakdown(cluster, run=1).image_pull_s == 264.0


def test_a_restarted_engine_container_fails_naming_its_restarts() -> None:
    cluster = RecordedCluster()
    cluster.pod["status"]["containerStatuses"][0]["restartCount"] = 2

    with pytest.raises(
        ColdStartMarkerError,
        match=r"container vllm of pod vllm-gpu-bcd494f9-xsjvl restarted 2 times",
    ):
        read_cold_start_breakdown(cluster, run=1)


@pytest.mark.parametrize("condition", ["Launched", "Initialized"])
def test_a_nodeclaim_missing_a_condition_fails_naming_it(condition: str) -> None:
    cluster = RecordedCluster()
    claim = cluster.nodeclaims[0]
    claim["status"]["conditions"] = [
        c for c in claim["status"]["conditions"] if c["type"] != condition
    ]

    with pytest.raises(
        ColdStartMarkerError, match=rf"NodeClaim gpu-xqwqj .*{condition}"
    ):
        read_cold_start_breakdown(cluster, run=1)


@pytest.mark.parametrize("reason", ["Pulling", "Pulled"])
def test_a_missing_pull_event_fails_naming_it(reason: str) -> None:
    cluster = RecordedCluster()
    cluster.events["items"] = [
        e for e in cluster.events["items"] if e["reason"] != reason
    ]

    with pytest.raises(
        ColdStartMarkerError,
        match=rf"no {reason} event for container vllm of pod vllm-gpu-bcd494f9-xsjvl.*expire",
    ):
        read_cold_start_breakdown(cluster, run=1)


def test_a_pod_that_never_became_ready_fails_naming_it() -> None:
    cluster = RecordedCluster()
    ready = next(c for c in cluster.pod["status"]["conditions"] if c["type"] == "Ready")
    ready["status"] = "False"

    with pytest.raises(
        ColdStartMarkerError, match=r"pod vllm-gpu-bcd494f9-xsjvl .*Ready"
    ):
        read_cold_start_breakdown(cluster, run=1)


def test_a_pod_with_no_hosting_nodeclaim_fails_naming_its_node() -> None:
    cluster = RecordedCluster()
    cluster.nodeclaims[0]["status"]["nodeName"] = (
        "ip-10-0-99-9.eu-west-1.compute.internal"
    )

    with pytest.raises(
        ColdStartMarkerError,
        match=r"no NodeClaim hosts node ip-10-0-17-228\.eu-west-1\.compute\.internal",
    ):
        read_cold_start_breakdown(cluster, run=1)


@pytest.mark.parametrize("count", [0, 2])
def test_anything_but_one_replica_pod_fails(count: int) -> None:
    cluster = RecordedCluster()
    cluster.pods = [cluster.pod] * count

    with pytest.raises(
        ColdStartMarkerError, match=rf"{count} pods match app=vllm-gpu in slipstream"
    ):
        read_cold_start_breakdown(cluster, run=1)


def test_markers_out_of_order_fail_naming_the_phase() -> None:
    cluster = RecordedCluster()
    pulled = next(e for e in cluster.events["items"] if e["reason"] == "Pulled")
    pulled["firstTimestamp"] = pulled["lastTimestamp"] = "2026-11-13T15:22:30Z"

    with pytest.raises(ColdStartRecordError, match=r"image_pull_s is -3\.0"):
        read_cold_start_breakdown(cluster, run=1)


def test_a_retried_pull_is_timed_from_its_first_pulling_event() -> None:
    cluster = RecordedCluster()
    pulling = next(e for e in cluster.events["items"] if e["reason"] == "Pulling")
    retry = deepcopy(pulling)
    retry["firstTimestamp"] = retry["lastTimestamp"] = "2026-11-13T15:24:00Z"
    cluster.events["items"].append(retry)

    assert read_cold_start_breakdown(cluster, run=1).image_pull_s == 264.0


@pytest.mark.parametrize(
    "marker",
    [
        "Time spent downloading weights",
        "Loading weights took",
        "init engine (profile, create kv cache, warmup model) took",
        "Starting vLLM server on",
        "torch.compile took",
        "Graph capturing finished in",
        "Initial profiling/warmup run took",
    ],
)
def test_a_missing_engine_log_marker_fails_naming_it(marker: str) -> None:
    cluster = RecordedCluster()
    cluster.engine_log = "\n".join(
        line for line in cluster.engine_log.splitlines() if marker not in line
    )

    with pytest.raises(
        ColdStartMarkerError,
        match=(
            rf"engine log of pod vllm-gpu-bcd494f9-xsjvl has no "
            rf"`{re.escape(marker)}` line"
        ),
    ):
        read_cold_start_breakdown(cluster, run=1)


_CACHE_HIT_LINE = (
    "2026-11-13T15:30:00.000000000Z (EngineCore pid=127) INFO 11-13 15:30:00 "
    "[backends.py:300] Directly load the compiled graph(s) for compile range "
    "(1, 2048) from the cache, took 3.120 s\n"
)


def test_the_recorded_cache_directory_line_is_not_a_compile_cache_hit() -> None:
    # vLLM logs ``Using cache directory`` on a miss too; the recorded run was a miss.
    cluster = RecordedCluster()
    assert "Using cache directory" in cluster.engine_log

    assert read_cold_start_breakdown(cluster, run=1).torch_compile_s == 44.91


def test_a_compile_cache_hit_fails_the_run() -> None:
    cluster = RecordedCluster()
    cluster.engine_log += _CACHE_HIT_LINE

    with pytest.raises(
        ColdStartCycleError,
        match=(
            r"engine log of pod vllm-gpu-bcd494f9-xsjvl shows a compile cache hit "
            r"\(`Directly load the compiled graph\(s\)`\)"
        ),
    ):
        read_cold_start_breakdown(cluster, run=1)


def test_a_pod_ready_in_the_second_its_server_started_has_no_readiness_lag() -> None:
    # Ready is recorded to the second, so the server's start is read to the second too.
    cluster = RecordedCluster()
    ready = next(c for c in cluster.pod["status"]["conditions"] if c["type"] == "Ready")
    ready["lastTransitionTime"] = "2026-11-13T15:30:41Z"

    assert read_cold_start_breakdown(cluster, run=1).readiness_lag_s == 0.0


def test_a_compile_cache_hit_is_named_over_a_missing_marker() -> None:
    cluster = RecordedCluster()
    cluster.engine_log = "\n".join(
        line
        for line in cluster.engine_log.splitlines()
        if "torch.compile took" not in line
    )
    cluster.engine_log += "\n" + _CACHE_HIT_LINE

    with pytest.raises(ColdStartCycleError, match="compile cache hit"):
        read_cold_start_breakdown(cluster, run=1)
