"""The kv-ab flow's terminal render: fold the A/B's records and publish its tables and chart.

Exercises the transport-free render core (drive_kv_ab_render) over the real report fold and
plotter, with the record reads, the artifact publishes, and the plot upload faked: it reads
every rung's record off the flow's pointers, publishes the spread and ceiling tables, then
uploads the chart and embeds it on the run page. The S3 record reader is exercised against a
fake S3 serving the record objects.
"""

import io
import json
from typing import Any

import pytest
from prefect.cache_policies import NO_CACHE

from slipstream_bench.contract import SweepAggregationError
from slipstream_bench.orchestration.tasks.kv_ab_render import (
    drive_kv_ab_render,
    read_kv_ab_record,
    render_kv_ab_results,
)
from tests.report.kv_ab_records import build_record

# A PNG file leads with this 8-byte signature.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# One run per arm over a two-rung ladder, keyed by the pointer the flow returns for each.
_RECORDS = {
    f"s3://bench/kv-ab/ab1/{arm}-r1/pshare90_burst1.0_mc{cap}.json": build_record(
        arm=arm, run=1, max_concurrency=cap
    )
    for arm in ("off", "on")
    for cap in (1, 2)
}


class _Render:
    """The render's faked collaborators and the log of what each was handed.

    Records are served from ``records``; the chart upload raises ``store_error`` when set.
    """

    def __init__(
        self,
        records: dict[str, dict[str, Any]] = _RECORDS,
        store_error: Exception | None = None,
    ) -> None:
        self._records = records
        self._store_error = store_error
        self.events: list[str] = []
        self.tables: dict[str, str] = {}
        self.stored: dict[str, bytes] = {}
        self.plots: list[dict[str, str]] = []

    def read_record(self, pointer: str) -> dict[str, Any]:
        self.events.append(f"read {pointer}")
        return self._records[pointer]

    def publish(self, *, key: str, markdown: str) -> None:
        self.events.append(f"table {key}")
        self.tables[key] = markdown

    def store(self, *, name: str, data: bytes) -> str:
        if self._store_error:
            raise self._store_error
        self.events.append(f"store {name}")
        self.stored[name] = data
        return f"sweeps/kv-ab/ab1/charts/{name}"

    def publish_plot(self, *, key: str, s3_key: str, heading: str, blurb: str) -> None:
        self.events.append(f"plot {key}")
        self.plots.append(
            {"key": key, "s3_key": s3_key, "heading": heading, "blurb": blurb}
        )

    def drive(self, pointers: list[str]) -> None:
        drive_kv_ab_render(
            pointers=pointers,
            read_record=self.read_record,
            publish=self.publish,
            store=self.store,
            publish_plot=self.publish_plot,
        )


def test_render_task_opts_out_of_caching() -> None:
    # The task takes the live S3 client and publish functions, which the default
    # inputs-hashing cache policy cannot hash.
    assert render_kv_ab_results.cache_policy is NO_CACHE


def test_the_render_reads_every_record_then_publishes_the_tables_then_the_chart() -> (
    None
):
    render = _Render()

    render.drive(list(_RECORDS))

    assert render.events == [
        *(f"read {pointer}" for pointer in _RECORDS),
        "table kv-ab-rung-spreads",
        "table kv-ab-ceilings",
        "store kv-ab-by-max-concurrency.png",
        "plot kv-ab-plot",
    ]
    assert render.stored["kv-ab-by-max-concurrency.png"].startswith(_PNG_MAGIC)
    (plot,) = render.plots
    assert plot["s3_key"] == "sweeps/kv-ab/ab1/charts/kv-ab-by-max-concurrency.png"


def test_a_run_with_no_records_fails_before_publishing_anything() -> None:
    render = _Render()

    with pytest.raises(ValueError, match="no rung records"):
        render.drive([])

    assert render.events == []


class _RecordS3:
    """Serve each record object's JSON body, as boto3's get_object returns it."""

    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self._objects = objects

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        return {"Body": io.BytesIO(self._objects[(Bucket, Key)])}


def test_a_record_is_read_off_its_pointer() -> None:
    record = build_record()
    s3 = _RecordS3({("bench", "kv-ab/ab1/on-r2/c.json"): json.dumps(record).encode()})

    assert (
        read_kv_ab_record("s3://bench/kv-ab/ab1/on-r2/c.json", s3_client=s3) == record
    )


@pytest.mark.parametrize(
    "pointer",
    [
        "bench/kv-ab/ab1/on-r2/c.json",
        "https://bench/kv-ab/ab1/on-r2/c.json",
        "s3://bench/",
    ],
)
def test_a_pointer_outside_s3_is_refused(pointer: str) -> None:
    with pytest.raises(ValueError, match="not an s3:// pointer"):
        read_kv_ab_record(pointer, s3_client=_RecordS3({}))


class _RunS3(_RecordS3):
    """Serve the run's records and keep each object the render puts."""

    def __init__(self) -> None:
        super().__init__(
            {
                ("bench", pointer.removeprefix("s3://bench/")): json.dumps(
                    record
                ).encode()
                for pointer, record in _RECORDS.items()
            }
        )
        self.puts: dict[str, tuple[bytes, str]] = {}

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str
    ) -> None:
        assert Bucket == "bench"
        self.puts[Key] = (Body, ContentType)


def test_the_task_uploads_the_chart_under_the_runs_public_charts_prefix() -> None:
    s3 = _RunS3()
    tables: list[str] = []
    images: list[dict[str, str]] = []

    render_kv_ab_results.fn(
        pointers=list(_RECORDS),
        run_id="ab1",
        bucket="bench",
        region="us-east-1",
        s3_client=s3,
        publish=lambda *, key, markdown: tables.append(key),
        publish_image=lambda **image: images.append(image),
    )

    key = "sweeps/kv-ab/ab1/charts/kv-ab-by-max-concurrency.png"
    body, content_type = s3.puts[key]
    assert (body[:8], content_type) == (_PNG_MAGIC, "image/png")
    assert tables == ["kv-ab-rung-spreads", "kv-ab-ceilings"]
    (image,) = images
    assert image["image_url"] == f"https://bench.s3.us-east-1.amazonaws.com/{key}"
    assert image["key"] == "kv-ab-plot"


def test_a_record_that_is_not_a_kv_ab_rung_fails_before_publishing_anything() -> None:
    pointer = "s3://bench/kv-ab/ab1/on-r1/pshare90_burst1.0_mc1.json"
    unlabelled = {k: v for k, v in _RECORDS[pointer].items() if k != "kv_ab"}
    render = _Render(records={pointer: unlabelled})

    with pytest.raises(SweepAggregationError, match="bench/kv-ab/ab1/on-r1/.*kv_ab"):
        render.drive([pointer])

    assert render.events == [f"read {pointer}"]


def test_a_failed_chart_upload_leaves_both_tables_published() -> None:
    render = _Render(store_error=OSError("upload failed"))

    with pytest.raises(OSError, match="upload failed"):
        render.drive(list(_RECORDS))

    assert list(render.tables) == ["kv-ab-rung-spreads", "kv-ab-ceilings"]
    assert render.plots == []


def test_the_ceiling_table_prints_each_arm_runs_ceiling() -> None:
    render = _Render()

    render.drive(list(_RECORDS))

    # Every rung holds the goodput floor, so each arm's single run tops out at rung 2.
    assert "| off | 2 | 2 |" in render.tables["kv-ab-ceilings"]
    assert "| on | 2 | 2 |" in render.tables["kv-ab-ceilings"]


def test_a_run_with_one_arm_fails_before_publishing_anything() -> None:
    on_only = {p: r for p, r in _RECORDS.items() if r["kv_ab"]["arm"] == "on"}
    render = _Render(records=on_only)

    with pytest.raises(SweepAggregationError, match="no off arm rungs"):
        render.drive(list(on_only))

    assert not any(event.startswith(("table", "store")) for event in render.events)


def test_a_record_that_cannot_be_read_names_its_pointer() -> None:
    with pytest.raises(KeyError) as raised:
        read_kv_ab_record("s3://bench/kv-ab/ab1/on-r2/c.json", s3_client=_RecordS3({}))

    assert raised.value.__notes__ == [
        "reading kv-ab record s3://bench/kv-ab/ab1/on-r2/c.json"
    ]


def test_the_spread_table_prints_each_arms_rung() -> None:
    render = _Render()

    render.drive(list(_RECORDS))

    # One run per arm, so each spread's range collapses onto its median.
    assert (
        "| off | 1 | 1 | 0.95 [0.95–0.95] | 760 [760–760] | 34 [34–34] "
        "| 131.0 [131.0–131.0] | 0.30 [0.30–0.30] | 0.85 [0.85–0.85] |"
    ) in render.tables["kv-ab-rung-spreads"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"{not json", "is not valid JSON"),
        (b'{"a": "\xff"}', "is not valid JSON"),
        (b"[]", "is not a JSON object"),
    ],
)
def test_a_record_object_that_is_not_a_json_object_names_its_pointer(
    body: bytes, message: str
) -> None:
    s3 = _RecordS3({("bench", "kv-ab/ab1/on-r2/c.json"): body})

    with pytest.raises(
        SweepAggregationError, match=f"s3://bench/kv-ab/ab1/on-r2/c.json {message}"
    ):
        read_kv_ab_record("s3://bench/kv-ab/ab1/on-r2/c.json", s3_client=s3)
