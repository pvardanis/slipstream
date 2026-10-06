"""The pure naming helpers the executor and the worker both read a cell's identity from.

``split_lengths`` turns a token budget and a prefix-share into the (prefix, suffix) split
one cell's workload runs; ``get_cell_basename`` names the result JSON a cell writes. Both
are pure derivations of a ``CellConfig``, shared so the executor that writes a cell's
result and the worker that addresses it by S3 object and cache key read one name from one
place (ADR-0012).
"""

from slipstream_bench.contract.config import CellConfig, SweepError


def split_lengths(total_len: int, share: int, *, align_blocks: int) -> tuple[int, int]:
    """Split a token budget into prefix and suffix lengths for one prefix-share.

    The prefix is floored to whole tokens (share 33 of 100 is a 33/67 split), and
    when ``align_blocks`` is set it is floored further to a multiple of that block
    size. vLLM's prefix cache hashes the prompt in fixed-size blocks and reuses
    whole blocks only, so a ragged tail recomputes every time and dilutes the
    cold/warm gap. Flooring (never rounding up) keeps the prefix within the budget,
    so the suffix stays non-negative.

    :param total_len: prefix + suffix token budget.
    :param share: prefix-share percentage in 0..100.
    :param align_blocks: block size to floor the prefix to; 0 disables alignment.
    :return: the (prefix_len, suffix_len) pair, summing to ``total_len``.
    :raise SweepError: when alignment floors a non-empty prefix to 0, which would
        erase the shared prefix the run exists to measure.
    """
    prefix_len = total_len * share // 100
    # A share of 0 asks for no prefix, which floors to 0 legitimately; a non-empty
    # prefix flooring to 0 would erase the shared prefix, so that fails fast.
    if align_blocks > 0 and prefix_len > 0:
        aligned = prefix_len // align_blocks * align_blocks
        if aligned == 0:
            raise SweepError(
                f"align_blocks {align_blocks} floors prefix {prefix_len} "
                f"(share {share}% of {total_len}) to 0 — raise total_len or the "
                f"prefix_shares entry, or lower align_blocks"
            )
        prefix_len = aligned
    return prefix_len, total_len - prefix_len


def get_cell_basename(cell: CellConfig) -> str:
    """Name the result JSON one cell writes, without its directory.

    A closed-loop cell carries its cap in the name so ladder rungs never collide;
    an open-loop cell (no cap) is left un-suffixed. The orchestration layer reads the
    same name to address the cell's S3 object and cache key, so the two never drift.
    """
    cap = f"_mc{cell.max_concurrency}" if cell.max_concurrency is not None else ""
    return f"pshare{cell.prefix_share}_burst{cell.burstiness}{cap}.json"
