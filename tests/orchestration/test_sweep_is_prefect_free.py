"""The ``sweep`` package's own source must name no Prefect import — the fast guard.

ADR-0012 §Amendment: the bench-client image installs ``.`` (Prefect-free) and its only
import path is ``load-sweep`` → ``sweep/``. If any ``sweep`` module imports ``prefect``
or ``prefect_aws``, that path pulls a dependency the image does not ship and the
container breaks at import. This walks every ``sweep`` source file's AST and fails on
any such import — a fast, precise check that names the offending file. It sees only
static ``import`` statements in ``sweep/``'s own files, not dynamic imports or the
transitive closure through other packages; ``test_import_contract`` covers that whole
contract by importing ``sweep`` in a Prefect-free interpreter. Prefect-touching code
lives in ``slipstream_bench.orchestration`` instead, imported lazily inside functions.
"""

import ast
from pathlib import Path

import slipstream_bench.sweep as sweep_pkg

_SWEEP_DIR = Path(sweep_pkg.__file__).parent
_PREFECT_ROOTS = frozenset({"prefect", "prefect_aws"})


def _imports_prefect(source: str) -> bool:
    """Return whether the source's AST holds any top-level or nested prefect import."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] in _PREFECT_ROOTS for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in _PREFECT_ROOTS:
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
