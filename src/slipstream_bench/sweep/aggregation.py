"""The error the knob sweep raises when an engine reports no concurrency ceiling.

The multi-cell aggregation that folds a run into ceiling and cliff rows is analysis, not
contract, and lives in the report member (:mod:`slipstream_bench.report.aggregation`).
What stays here is the one orchestration-raised error: the parent knob sweep scrapes each
redeployed engine's reported ceiling before running the point's Tier-2 ladder (ADR-0015),
and a scrape that finds none raises :class:`CeilingScrapeError`.
"""


class CeilingScrapeError(Exception):
    """No concurrency ceiling could be scraped for an engine point.

    The parent knob sweep scrapes each redeployed engine's reported ceiling before
    running the point's Tier-2 ladder (ADR-0015). A scrape that finds none raises this
    rather than returning empty, so the point fails loudly instead of measuring its
    ladder against a garbage ceiling — the one failure an unattended sweep cannot
    tolerate.
    """
