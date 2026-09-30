"""The parent knob-sweep flow: drive one point sweep per engine point (ADR-0015).

The outer loop over the grid's engine points as one Prefect flow. Per point it redeploys
the GPU, scrapes the concurrency ceiling (fail-loud), then runs the existing per-point
sweep as a nested subflow — with a resume gate that skips a point whose cells already
hold valid measurements. Collaborators are injected, so the whole path is exercised here
against fakes under ``prefect_test_harness()``: no Cloud, cluster, or GPU needed.
"""

from collections.abc import Iterator

import pytest
from prefect.testing.utilities import prefect_test_harness

from slipstream_bench.sweep.aggregation import CeilingScrapeError, EnginePoint

_P1 = EnginePoint(max_num_seqs=64, kv_cache_dtype="fp8", prefix_caching=True)
_P2 = EnginePoint(max_num_seqs=128, kv_cache_dtype="fp16", prefix_caching=False)


@pytest.fixture(scope="module", autouse=True)
def _harness() -> Iterator[None]:
    with prefect_test_harness():
        yield


def _collaborators(
    events: list[str],
    *,
    pending: set[str] | None = None,
    empty_ceiling: set[str] | None = None,
):
    """Build the four injected collaborators over a shared per-call event log.

    ``pending`` names the slugs the redeploy-skip gate reports as still having cells to
    run; when omitted every point is pending. ``empty_ceiling`` names the slugs whose
    scrape finds no ceiling and so raises. Each collaborator appends ``op:slug`` so a
    test asserts both which points were driven and the deploy->scrape->sweep order.
    """

    def deploy_fn(point: EnginePoint) -> None:
        events.append(f"deploy:{point.slug()}")

    def scrape_fn(point: EnginePoint) -> None:
        events.append(f"scrape:{point.slug()}")
        if empty_ceiling is not None and point.slug() in empty_ceiling:
            raise CeilingScrapeError(f"no ceiling scraped for {point.slug()}")

    def point_sweep_fn(point: EnginePoint) -> list[str]:
        events.append(f"sweep:{point.slug()}")
        return [f"ptr:{point.slug()}"]

    def has_pending_cells(point: EnginePoint) -> bool:
        return pending is None or point.slug() in pending

    return deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells


def test_drives_each_pending_point_deploy_then_scrape_then_sweep() -> None:
    from slipstream_bench.orchestration.flows.knob_sweep import run_knob_sweep

    events: list[str] = []
    deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells = _collaborators(events)

    pointers = run_knob_sweep(
        points=[_P1, _P2],
        deploy_fn=deploy_fn,
        scrape_fn=scrape_fn,
        point_sweep_fn=point_sweep_fn,
        has_pending_cells=has_pending_cells,
    )

    assert pointers == [f"ptr:{_P1.slug()}", f"ptr:{_P2.slug()}"]
    assert events == [
        f"deploy:{_P1.slug()}",
        f"scrape:{_P1.slug()}",
        f"sweep:{_P1.slug()}",
        f"deploy:{_P2.slug()}",
        f"scrape:{_P2.slug()}",
        f"sweep:{_P2.slug()}",
    ]


def test_a_fully_valid_point_skips_its_redeploy_and_scrape_but_still_reports() -> None:
    from slipstream_bench.orchestration.flows.knob_sweep import run_knob_sweep

    events: list[str] = []
    deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells = _collaborators(
        events, pending={_P2.slug()}
    )

    pointers = run_knob_sweep(
        points=[_P1, _P2],
        deploy_fn=deploy_fn,
        scrape_fn=scrape_fn,
        point_sweep_fn=point_sweep_fn,
        has_pending_cells=has_pending_cells,
    )

    # P1 is fully valid: the two expensive tasks — its ~20-minute redeploy and the ceiling
    # scrape — are not re-paid to run zero cells (ADR-0015).
    assert f"deploy:{_P1.slug()}" not in events
    assert f"scrape:{_P1.slug()}" not in events
    # But its resumable sweep still runs, hitting the cache and returning P1's pointers,
    # so every point is reported: skipping the redeploy must not drop cached measurements.
    assert events == [
        f"sweep:{_P1.slug()}",
        f"deploy:{_P2.slug()}",
        f"scrape:{_P2.slug()}",
        f"sweep:{_P2.slug()}",
    ]
    assert pointers == [f"ptr:{_P1.slug()}", f"ptr:{_P2.slug()}"]


def test_an_empty_ceiling_scrape_raises_and_the_points_cells_never_run() -> None:
    from slipstream_bench.orchestration.flows.knob_sweep import run_knob_sweep

    events: list[str] = []
    deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells = _collaborators(
        events, empty_ceiling={_P1.slug()}
    )

    with pytest.raises(
        CeilingScrapeError, match=f"no ceiling scraped for {_P1.slug()}"
    ):
        run_knob_sweep(
            points=[_P1, _P2],
            deploy_fn=deploy_fn,
            scrape_fn=scrape_fn,
            point_sweep_fn=point_sweep_fn,
            has_pending_cells=has_pending_cells,
        )

    # The scrape found no ceiling, so the point's Tier-2 ladder never runs against a
    # garbage ceiling: P1 is deployed and scraped, then the loud failure aborts the whole
    # sweep — no sweep call for P1, and nothing at all for P2.
    assert events == [f"deploy:{_P1.slug()}", f"scrape:{_P1.slug()}"]


def test_no_points_drives_no_collaborators_and_returns_no_pointers() -> None:
    from slipstream_bench.orchestration.flows.knob_sweep import run_knob_sweep

    events: list[str] = []
    deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells = _collaborators(events)

    pointers = run_knob_sweep(
        points=[],
        deploy_fn=deploy_fn,
        scrape_fn=scrape_fn,
        point_sweep_fn=point_sweep_fn,
        has_pending_cells=has_pending_cells,
    )

    # An empty grid drives nothing and reports nothing — no collaborator fires.
    assert pointers == []
    assert events == []


def test_every_point_sweep_runs_nested_under_the_parent_flow() -> None:
    from slipstream_bench.orchestration.flows.knob_sweep import run_knob_sweep

    flow_names: list[str | None] = []

    def recording_point_sweep(point: EnginePoint) -> list[str]:
        from prefect.runtime import flow_run

        flow_names.append(flow_run.flow_name)
        return [f"ptr:{point.slug()}"]

    run_knob_sweep(
        points=[_P1, _P2],
        deploy_fn=lambda _p: None,
        scrape_fn=lambda _p: None,
        point_sweep_fn=recording_point_sweep,
        has_pending_cells=lambda _p: True,
    )

    # Each point sweep observes the one parent knob-sweep flow run: real parent->child
    # lineage — every point sweep nests under the same parent run.
    assert flow_names == ["knob-sweep", "knob-sweep"]
