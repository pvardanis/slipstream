"""The shared value objects and the parse of one result record into them.

``EnginePoint`` names one Tier-1 engine-knob point (and the slug the grid emits, the recipe
writes, and the aggregator parses); ``LoadCell`` is one Tier-2 ladder rung parsed from a
single ``vllm bench serve`` result, with its goodput fraction and failure cohorts. Both are
frozen value objects, and ``LoadCell.from_record`` (with ``goodput_fraction`` and
``classify_failures``) is the deserialization of one result record — the kernel both the
executor's aggregation and the worker's validity check parse a record through, so the two
read one record shape from one place. The multi-cell fold over a run lives in the analysis
layer, not here.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

from slipstream.contract.results import to_numeric_metric

# The KV-cache dtype labels the sweep varies: fp8 committed, fp16 the counterfactual
# baseline. An EnginePoint names one of them, and the grid constrains its dtype axis to
# the same set, so the value object is no weaker than the grid that feeds it.
KvLabel = Literal["fp8", "fp16"]

# A point subdir is mns{N}_kv{fp8|fp16}_pc{on|off} — the engine knobs one Tier-1
# redeploy was rendered with, encoded in the name the recipe nests its JSON under.
_POINT_PATTERN = re.compile(r"^mns(?P<mns>\d+)_kv(?P<kv>fp8|fp16)_pc(?P<pc>on|off)$")

# vLLM stores a failed request's error as the formatted exception traceback, so a
# client-side deadline miss (asyncio.TimeoutError) writes the type name — and thus
# this lowercased marker — into the error text. The match is a substring over the
# whole traceback, so any error text containing "timeout" counts as timeout; every
# other non-empty error is a hard failure of an unclassified kind.
_TIMEOUT_MARKER = "timeout"


class SweepAggregationError(Exception):
    """A sweep artifact that cannot be aggregated into a ceiling row."""


class FailureCohorts(TypedDict):
    """The failed-request cohorts summed across a ceiling row's rungs.

    ``oom`` stays ``None`` when the sweep collected no OOMKilled event and engine-log
    scrape — a not-captured cohort, distinct from a measured zero.
    """

    timeout: int
    other: int
    oom: int | None


@dataclass(frozen=True)
class EnginePoint:
    """The engine-knob point one Tier-1 redeploy measured.

    The three engine knobs the sweep varies per redeploy: the batch cap
    (max-num-seqs), the KV-cache dtype (fp8 committed, fp16 the counterfactual
    baseline), and whether prefix caching was on. Binding them into one value object
    keeps the aggregation key from travelling as three loose values a caller could
    transpose.
    """

    max_num_seqs: int
    kv_cache_dtype: KvLabel
    prefix_caching: bool

    @classmethod
    def from_dirname(cls, name: str) -> "EnginePoint":
        """Parse a point off its sweep subdir name.

        :param name: the subdir the recipe nested a point's JSON under, e.g.
            ``mns64_kvfp8_pcon``.
        :return: the engine-knob point it names.
        :raise SweepAggregationError: when the name is not a valid point — a stray
            directory would otherwise aggregate as a nonsense key.
        """
        match = _POINT_PATTERN.match(name)
        if match is None:
            raise SweepAggregationError(
                f"'{name}' is not a knob-sweep point subdir "
                f"(want mns<N>_kv<fp8|fp16>_pc<on|off>)"
            )
        return cls(
            max_num_seqs=int(match["mns"]),
            # The pattern's kv group matches only fp8|fp16, so the capture is a KvLabel.
            kv_cache_dtype=cast(KvLabel, match["kv"]),
            prefix_caching=match["pc"] == "on",
        )

    def slug(self) -> str:
        """Name the subdir this point's Tier-2 JSON is nested under.

        The inverse of ``from_dirname``: the single builder of the
        ``mns{N}_kv{dtype}_pc{on|off}`` format the grid emits, the recipe writes,
        and the aggregator parses — so all three read one format from one place.

        :return: the point's subdir name, e.g. ``mns64_kvfp8_pcon``.
        """
        caching = "on" if self.prefix_caching else "off"
        return f"mns{self.max_num_seqs}_kv{self.kv_cache_dtype}_pc{caching}"

    @staticmethod
    def is_point_dirname(name: str) -> bool:
        """Return whether ``name`` is a point subdir name :meth:`from_dirname` parses.

        The predicate a run-dir scan filters on before parsing: a run directory also
        holds a predicted-ceilings ledger and a charts subdir, so a caller keeps only
        the point subdirs rather than letting a stray name reach ``from_dirname``.
        """
        return _POINT_PATTERN.match(name) is not None


def goodput_fraction(record: dict[str, object], source: Path) -> float:
    """Read the fraction of a cell's completed requests that met the SLO.

    vLLM reports goodput and throughput as rates (req/s) over the same run window,
    so their ratio is the fraction of completed requests meeting both the ttft and
    tpot thresholds — the number the 95% ceiling test compares against. A cell that
    completed nothing has zero throughput and held no goodput, so its fraction is
    0.0 rather than an undefined 0/0.

    :param record: the cell's parsed ``vllm bench serve --save-result`` record.
    :param source: the cell's result file, for the error message.
    :return: the goodput fraction, 0.0 when the cell completed nothing.
    :raise SweepAggregationError: when either rate is absent, null, non-numeric,
        non-finite, or negative.
    """
    goodput = to_numeric_metric(
        record, source, "request_goodput", error_cls=SweepAggregationError
    )
    throughput = to_numeric_metric(
        record, source, "request_throughput", error_cls=SweepAggregationError
    )
    if throughput == 0:
        return 0.0
    return goodput / throughput


def classify_failures(record: dict[str, object], source: Path) -> FailureCohorts:
    """Cohort a cell's failed requests into {timeout, oom, other} (ADR-0009).

    ``--save-detailed`` records one error string per request (empty on success), so
    a present ``errors`` list is split by matching a deadline marker to *timeout*
    and sending any other non-empty error to *other*. A result written without
    ``--save-detailed`` carries no per-request errors, so its failures — completed
    short of attempted — fall to *other*, their kind unknown. *oom* is always
    ``None``: it needs the pod ``OOMKilled`` event and engine-log scrape the sweep
    recipe does not collect, and an invented zero would read as measured-and-none.

    :param record: the cell's parsed result record.
    :param source: the cell's result file, for the error message.
    :return: the cohort counts ``{"timeout": int, "other": int, "oom": None}``.
    :raise SweepAggregationError: when ``errors`` is present but not a list, or holds a
        non-string entry — a malformed field cannot be cohorted.
    """
    errors = record.get("errors")
    if errors is None:
        return _get_cohorts_from_shortfall(record, source)
    if not isinstance(errors, list):
        raise SweepAggregationError(
            f"result {source} has a non-list errors field: cannot cohort failures"
        )
    if any(entry and not isinstance(entry, str) for entry in errors):
        raise SweepAggregationError(
            f"result {source} has a non-string errors entry: cannot cohort failures"
        )
    timeout = sum(1 for error in errors if error and _TIMEOUT_MARKER in error.lower())
    other = sum(1 for error in errors if error) - timeout
    return {"timeout": timeout, "other": other, "oom": None}


def _get_cohorts_from_shortfall(
    record: dict[str, object], source: Path
) -> FailureCohorts:
    """Cohort failures a no-detail result only knows as attempted-minus-completed.

    Without a per-request ``errors`` array the kind of each failure is unknown, so
    the shortfall of completed requests below the prompts attempted is charged to
    *other* — never silently dropped, never guessed as timeouts.

    :param record: the cell's parsed result record.
    :param source: the cell's result file, for the error message.
    :return: the cohort counts, the whole shortfall in *other*.
    :raise SweepAggregationError: when either count is absent, null, or not a whole
        number, or completed exceeds attempted — a broken result must not read as a
        clean zero-failure cell.
    """
    attempted = _require_int(record, source, "num_prompts")
    completed = _require_int(record, source, "completed")
    if completed > attempted:
        raise SweepAggregationError(
            f"result {source} completed {completed} of {attempted} attempted: "
            f"inconsistent counts"
        )
    return {"timeout": 0, "other": attempted - completed, "oom": None}


@dataclass(frozen=True)
class LoadCell:
    """One Tier-2 client-load ladder rung: an offered concurrency and how it held up.

    The two client knobs the ladder varies without a redeploy — the closed-loop
    ``--max-concurrency`` the rung offered and the prefix-share it ran — plus the
    fraction of its completed requests that met the SLO and its failure cohorts.
    """

    max_concurrency: int
    prefix_share: int
    goodput_fraction: float
    failures: FailureCohorts

    @classmethod
    def from_record(cls, record: dict[str, object], source: Path) -> "LoadCell":
        """Build a cell from a parsed client JSON, validating each field at the seam.

        :param record: the cell's parsed ``vllm bench serve --save-result`` record.
        :param source: the cell's result file, for the error message.
        :return: the cell's offered concurrency, prefix-share, goodput fraction, and
            failure cohorts.
        :raise SweepAggregationError: when the cell lacks its closed-loop cap or its
            stamped prefix-share, or carries a bad goodput or errors field.
        """
        return cls(
            max_concurrency=_require_int(record, source, "max_concurrency"),
            prefix_share=_require_int(record, source, "prefix_share"),
            goodput_fraction=goodput_fraction(record, source),
            failures=classify_failures(record, source),
        )


def _require_int(record: dict[str, object], source: Path, key: str) -> int:
    """Read a whole-number join key a ladder cell must carry.

    :param record: the cell's parsed result record.
    :param source: the cell's result file, for the error message.
    :param key: the field to read as an int.
    :return: the field as an int.
    :raise SweepAggregationError: when the field is absent, null, or not a whole number
        — bool is an int subclass but never a valid cap or share.
    """
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SweepAggregationError(f"result {source} missing or non-integer {key}")
    return value
