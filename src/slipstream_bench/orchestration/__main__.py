"""Console entry for ``slipstream-orchestrate``: guard the extra, then run the CLI.

ADR-0012 §Amendment: the orchestration driver needs Prefect and boto3, which ship only
in the ``orchestration`` extra. The ``slipstream-orchestrate`` script is installed by a
bare ``.`` too, so this is the one place the extra is checked: it verifies the required
distributions are importable before pulling in the Prefect-heavy CLI, turning a raw
``ImportError`` into one actionable message that names the extra. Because the check must
run before those distributions are imported, the CLI import is deferred into
:func:`main` — the single deferred import the layer keeps, in place of guarding every
module.
"""

from __future__ import annotations

import importlib.util

# The distributions the orchestration CLI imports at module load; all ship in the
# ``orchestration`` extra and none in a bare install.
_REQUIRED_MODULES = ("prefect", "prefect_aws", "boto3")


def main() -> None:
    """Verify the orchestration extra is installed, then run the CLI app."""
    _require_orchestration_extra()
    from slipstream_bench.orchestration.cli import app

    app()


def _require_orchestration_extra() -> None:
    """Exit with an actionable message when the orchestration extra is absent.

    :raise SystemExit: when any required distribution is not importable, naming the
        missing ones and the extra to install.
    """
    missing = [
        name for name in _REQUIRED_MODULES if importlib.util.find_spec(name) is None
    ]
    if missing:
        raise SystemExit(
            "slipstream-orchestrate needs the 'orchestration' extra "
            f"(missing: {', '.join(missing)}); install it with "
            "pip install 'slipstream-bench[orchestration]'"
        )


if __name__ == "__main__":
    main()
