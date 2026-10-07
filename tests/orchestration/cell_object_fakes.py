"""A fake S3 serving a knob-sweep run's cell objects, each record keyed off its basename.

Shared by the render tests and the knob-sweep flow test: ``materialize_run`` and
``point_is_complete`` download a run's cells by S3 key, so a fake that writes a valid,
SLO-passing cell record for every key — its prefix_share and max_concurrency read back off
the object's ``pshare{N}_burst{b}_mc{cap}`` basename — lets both fold a materialized run into
the grid's own points with no real S3. The keys read are recorded so a test asserts which
cells were materialized.
"""

import json
import re
from pathlib import Path

# A cell's basename stamps its coordinate (pshare{N}_burst{b}[_mc{cap}].json); the fake reads
# the share and cap back off the key so a materialized cell parses as the rung it names.
_BASENAME_COORDS = re.compile(r"pshare(?P<share>\d+)_burst[\d.]+(?:_mc(?P<cap>\d+))?")


class ServingCellS3:
    """Serve a valid, SLO-passing cell record for every key, its coordinate read off the key.

    The record's prefix_share and max_concurrency are read off the object key, so a
    materialized cell parses as the rung its basename names — the aggregators fold the run
    into the grid's own points rather than one collapsed coordinate. Records the keys read.
    """

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.uploads: list[tuple[str, str]] = []

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str
    ) -> None:
        # The render uploads each plot's PNG as its durable copy; record the (key, type) so a
        # test can assert the uploads without a real bucket.
        self.uploads.append((Key, ContentType))

    def download_file(self, _bucket: str, key: str, dest: str) -> None:
        self.keys.append(key)
        match = _BASENAME_COORDS.search(key)
        assert match is not None, key
        record = {
            "prefix_share": int(match["share"]),
            "max_concurrency": int(match["cap"]),
            "request_goodput": 1.0,
            "request_throughput": 1.0,
            "p95_ttft_ms": 850.0,
            "p95_tpot_ms": 42.0,
            "output_throughput": 1234.5,
            "errors": [""],
        }
        Path(dest).write_text(json.dumps(record), encoding="utf-8")
