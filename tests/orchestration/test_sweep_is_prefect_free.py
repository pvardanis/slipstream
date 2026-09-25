"""The ``sweep`` package must never import Prefect — the container's import contract.

ADR-0012 §Amendment: the bench-client image installs ``.`` (Prefect-free) and its only
import path is ``load-sweep`` → ``sweep/``. If any ``sweep`` module imports ``prefect``
or ``prefect_aws``, that path pulls a dependency the image does not ship and the
container breaks at import. This walks every ``sweep`` source file's AST and fails on
any such import, so the split cannot silently regress. Prefect-touching code lives in
``slipstream_bench.orchestration`` instead, imported lazily inside functions.
"""

import ast
from pathlib import Path

import slipstream_bench.sweep as sweep_pkg

_SWEEP_DIR = Path(sweep_pkg.__file__).parent


def _imports_prefect(source: str) -> bool:
    """Return whether the source's AST holds any top-level or nested prefect import."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name.split(".")[0].startswith("prefect") for alias in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0].startswith("prefect"):
                return True
    return False


def test_no_sweep_module_imports_prefect() -> None:
    offenders = [
        str(path.relative_to(_SWEEP_DIR))
        for path in sorted(_SWEEP_DIR.rglob("*.py"))
        if _imports_prefect(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        f"sweep modules import prefect: {offenders}; the bench-client image ships no "
        "prefect, so prefect-touching code must live in slipstream_bench.orchestration"
    )
