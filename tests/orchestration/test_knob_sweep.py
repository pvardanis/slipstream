"""The knob-sweep sequencing driver: one point sweep per engine point (ADR-0015).

Exercises drive_knob_sweep — the transport-free driver the parent flow wraps. The four
collaborators (GPU redeploy, ceiling scrape, per-point sweep, redeploy-skip gate) are
injected as fakes, so the whole path is asserted with no cluster, GPU, or Prefect server:
each pending point is redeployed then scraped then swept in that order, a fully-valid
point skips its redeploy and scrape but still reports its cached pointers, and an empty
ceiling scrape raises loud before the point's ladder runs.
"""

import logging

import pytest

from slipstream_bench.contract import EnginePoint
from slipstream_bench.orchestration.cluster import CeilingScrapeError
from slipstream_bench.orchestration.flows.knob_sweep import (
    SweepOutcome,
    build_sweep_summary,
    drive_knob_sweep,
)

_P1 = EnginePoint(max_num_seqs=64, kv_cache_dtype="fp8", prefix_caching=True)
_P2 = EnginePoint(max_num_seqs=128, kv_cache_dtype="fp16", prefix_caching=False)


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
    events: list[str] = []
    deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells = _collaborators(events)

    outcome = drive_knob_sweep(
        points=[_P1, _P2],
        deploy_fn=deploy_fn,
        scrape_fn=scrape_fn,
        point_sweep_fn=point_sweep_fn,
        has_pending_cells=has_pending_cells,
    )

    assert outcome.pointers == [f"ptr:{_P1.slug()}", f"ptr:{_P2.slug()}"]
    assert events == [
        f"deploy:{_P1.slug()}",
        f"scrape:{_P1.slug()}",
        f"sweep:{_P1.slug()}",
        f"deploy:{_P2.slug()}",
        f"scrape:{_P2.slug()}",
        f"sweep:{_P2.slug()}",
    ]


def test_a_fully_valid_point_skips_its_redeploy_and_scrape_but_still_reports() -> None:
    events: list[str] = []
    deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells = _collaborators(
        events, pending={_P2.slug()}
    )

    outcome = drive_knob_sweep(
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
    assert outcome.pointers == [f"ptr:{_P1.slug()}", f"ptr:{_P2.slug()}"]
    # P1 resumed from cache (no redeploy), P2 ran this invocation, so the run/resume split
    # the summary reports counts each point's cells on the side it was swept from.
    assert outcome.cells_run == 1
    assert outcome.cells_resumed == 1


def test_an_empty_ceiling_scrape_raises_and_the_points_cells_never_run() -> None:
    events: list[str] = []
    deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells = _collaborators(
        events, empty_ceiling={_P1.slug()}
    )

    with pytest.raises(
        CeilingScrapeError, match=f"no ceiling scraped for {_P1.slug()}"
    ):
        drive_knob_sweep(
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
    events: list[str] = []
    deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells = _collaborators(events)

    outcome = drive_knob_sweep(
        points=[],
        deploy_fn=deploy_fn,
        scrape_fn=scrape_fn,
        point_sweep_fn=point_sweep_fn,
        has_pending_cells=has_pending_cells,
    )

    # An empty grid drives nothing and reports nothing — no collaborator fires.
    assert outcome.pointers == []
    assert outcome.cells_run == 0
    assert outcome.cells_resumed == 0
    assert events == []


def test_each_point_logs_a_done_milestone_with_its_run_or_resume_split(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    deploy_fn, scrape_fn, point_sweep_fn, has_pending_cells = _collaborators(
        events, pending={_P2.slug()}
    )

    with caplog.at_level(
        logging.INFO, logger="slipstream_bench.orchestration.flows.knob_sweep"
    ):
        drive_knob_sweep(
            points=[_P1, _P2],
            deploy_fn=deploy_fn,
            scrape_fn=scrape_fn,
            point_sweep_fn=point_sweep_fn,
            has_pending_cells=has_pending_cells,
        )

    # Each point ends with a milestone naming it, its cell count, and whether its cells
    # ran this invocation or resumed from cache — the per-point progress an operator reads
    # off the Prefect run page as the sweep works through the grid (ADR-0020).
    assert f"{_P1.slug()}" in caplog.text
    assert f"{_P2.slug()}" in caplog.text
    assert "resumed" in caplog.text
    assert "run" in caplog.text


def test_build_sweep_summary_reports_the_run_and_its_cell_split() -> None:
    # The pure builder for the flow's final-summary line: the run id it is filed under and
    # the run/resume cell split, so the one line carrying logic is tested without a flow
    # context or a Prefect server (ADR-0020).
    summary = build_sweep_summary(
        "run1", SweepOutcome(pointers=["a", "b", "c"], cells_run=2, cells_resumed=1)
    )

    assert "run1" in summary
    assert "3" in summary  # total cells
    assert "2" in summary  # run this invocation
    assert "1" in summary  # resumed from cache
