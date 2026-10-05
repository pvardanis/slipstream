"""Each boundary-rule package's own source names none of its forbidden imports — the fast guard.

The workspace keeps framework-free and member-isolated packages (ADR-0012 §Amendment,
ADR-0017): the bench-client image installs a Prefect-free path, and the ``contract`` kernel
depends on no framework and no other member. This walks every source file of each package in
``BOUNDARY_RULES`` and fails on any static ``import`` of a root the rule forbids — a fast,
precise check that names the offending file. It sees only static ``import`` statements in a
package's own files, not dynamic imports or the transitive closure through other packages;
``test_import_contract`` covers that whole closure by importing each package with its
forbidden roots blocked.
"""

import ast
import importlib
from pathlib import Path

import pytest

from tests.boundary_rules import BOUNDARY_RULES, BoundaryRule


def _imports_forbidden_root(source: str, forbidden: frozenset[str]) -> bool:
    """Return whether the source's AST holds any import of a forbidden top-level root."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] in forbidden for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in forbidden:
                return True
    return False


def _package_dir(package: str) -> Path:
    """Return the directory holding a package's own source files."""
    module_file = importlib.import_module(package).__file__
    assert module_file is not None, f"{package} has no __file__ to walk"
    return Path(module_file).parent


@pytest.mark.parametrize("rule", BOUNDARY_RULES, ids=lambda rule: rule.package)
def test_package_source_imports_nothing_forbidden(rule: BoundaryRule) -> None:
    package_dir = _package_dir(rule.package)
    offenders = [
        str(path.relative_to(package_dir))
        for path in sorted(package_dir.rglob("*.py"))
        if _imports_forbidden_root(path.read_text(encoding="utf-8"), rule.forbidden)
    ]

    assert offenders == [], (
        f"{rule.package} modules import a forbidden root "
        f"{sorted(rule.forbidden)}: {offenders}"
    )
