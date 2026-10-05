"""The contract kernel's public surface: the config-in/result-out types every member shares.

Re-exports the config, grid, result, and value-object symbols the executor and the worker
both depend on, so a caller writes ``from slipstream.contract import CellConfig`` without
reaching into a submodule. The kernel holds pure data and the parse of one record only; no
framework, no other workspace member (ADR-0017).
"""

from slipstream.contract.config import (
    RESERVED_KEYS,
    CellConfig,
    Knobs,
    LoadKnobs,
    SweepConfig,
    SweepError,
)
from slipstream.contract.grid import (
    SweepGrid,
    SweepGridError,
    build_point_sweep_config,
    list_engine_points,
    load_grid,
)
from slipstream.contract.naming import get_cell_basename, split_lengths
from slipstream.contract.records import (
    EnginePoint,
    FailureCohorts,
    LoadCell,
    SweepAggregationError,
    classify_failures,
    goodput_fraction,
)
from slipstream.contract.results import ResultError, read_result, to_numeric_metric

__all__ = [
    "RESERVED_KEYS",
    "CellConfig",
    "EnginePoint",
    "FailureCohorts",
    "Knobs",
    "LoadCell",
    "LoadKnobs",
    "ResultError",
    "SweepAggregationError",
    "SweepConfig",
    "SweepError",
    "SweepGrid",
    "SweepGridError",
    "build_point_sweep_config",
    "classify_failures",
    "get_cell_basename",
    "goodput_fraction",
    "list_engine_points",
    "load_grid",
    "read_result",
    "split_lengths",
    "to_numeric_metric",
]
