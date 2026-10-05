"""Each boundary-rule package's own source imports within its boundary — the fast guard.

The workspace keeps framework-free and member-isolated packages (ADR-0012 §Amendment,
ADR-0017): the bench-client image installs a Prefect-free path, and the ``contract`` kernel
depends on no framework and no other member. This walks every source file of each package in
``BOUNDARY_RULES`` and fails on any static ``import`` that breaches the rule — a root the rule
forbids, or (when the rule names an ``allowed`` set) any root outside it — a fast, precise
check that names the offending file. It sees only static ``import`` statements in a package's
own files, not dynamic imports or the transitive closure through other packages;
``test_import_contract`` covers that whole closure by importing each package with its
forbidden roots blocked.
"""

import ast
import importlib
from pathlib import Path

import pytest

from tests.boundary_rules import BOUNDARY_RULES, BoundaryRule


def _imported_roots(source: str) -> set[str]:
    """Return the top-level root of every absolute import in the source's AST.

    Relative imports (``from . import x``) are a package's own submodules, never an
    external dependency, so they are skipped rather than counted as an empty root.
    """
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots.add((node.module or "").split(".")[0])
    return roots


def _offending_roots(source: str, rule: BoundaryRule) -> set[str]:
    """Return the imported roots that breach the rule.

    Roots outside the ``allowed`` set when the rule names one (a closed contract), else
    the roots the rule forbids (an open set minus a few).
    """
    roots = _imported_roots(source)
    if rule.allowed is not None:
        return roots - rule.allowed
    return roots & rule.forbidden


def _package_dir(package: str) -> Path:
    """Return the directory holding a package's own source files."""
    module_file = importlib.import_module(package).__file__
    assert module_file is not None, f"{package} has no __file__ to walk"
    return Path(module_file).parent


@pytest.mark.parametrize("rule", BOUNDARY_RULES, ids=lambda rule: rule.package)
def test_package_source_imports_within_boundary(rule: BoundaryRule) -> None:
    package_dir = _package_dir(rule.package)
    offenders = {
        str(path.relative_to(package_dir)): sorted(breaches)
        for path in sorted(package_dir.rglob("*.py"))
        if (breaches := _offending_roots(path.read_text(encoding="utf-8"), rule))
    }

    assert offenders == {}, (
        f"{rule.package} modules import roots outside their boundary: {offenders}"
    )
