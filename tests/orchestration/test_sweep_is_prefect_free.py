"""Each boundary-rule package's own source imports within its boundary — the fast guard.

The workspace keeps framework-free and member-isolated packages (ADR-0012 §Amendment,
ADR-0017): the bench-client image installs a Prefect-free path, and the ``contract`` kernel
depends on no framework and no other member. This walks every source file of each package in
``BOUNDARY_RULES`` (or a module rule's one file) and fails on any static ``import`` that
breaches the rule — a root the rule forbids, or (when the rule names an ``allowed`` set) any
root outside it — a fast, precise check that names the offending file. It sees only static ``import`` statements in a package's
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


def _source_files(package: str) -> tuple[Path, list[Path]]:
    """Return the root a rule's source files are named from, and the files themselves.

    A package rule covers every file under the package's directory; a module rule covers
    the module's own file alone, not the package around it.
    """
    module_file = importlib.import_module(package).__file__
    assert module_file is not None, f"{package} has no __file__ to walk"
    path = Path(module_file)
    if path.name != "__init__.py":
        return path.parent, [path]
    return path.parent, sorted(path.parent.rglob("*.py"))


@pytest.mark.parametrize("rule", BOUNDARY_RULES, ids=lambda rule: rule.package)
def test_package_source_imports_within_boundary(rule: BoundaryRule) -> None:
    root, files = _source_files(rule.package)
    offenders = {
        str(path.relative_to(root)): sorted(breaches)
        for path in files
        if (breaches := _offending_roots(path.read_text(encoding="utf-8"), rule))
    }

    assert offenders == {}, (
        f"{rule.package} modules import roots outside their boundary: {offenders}"
    )


def test_imported_roots_reads_absolute_roots_and_skips_relative() -> None:
    """The breach check rests on this: absolute roots counted, relative imports skipped."""
    source = (
        "import os\n"
        "import a.b.c\n"
        "from pkg.sub import thing\n"
        "from . import sibling\n"
        "from ..other import thing\n"
    )
    assert _imported_roots(source) == {"os", "a", "pkg"}


def test_offending_roots_allowlist_flags_roots_outside_allowed() -> None:
    """A rule with an allowed set flags any import outside it, so a stray dep cannot slip."""
    rule = BoundaryRule(
        package="x",
        forbidden=frozenset(),
        probe="",
        allowed=frozenset({"os", "pydantic"}),
    )
    assert _offending_roots("import os\nimport numpy\n", rule) == {"numpy"}
    assert (
        _offending_roots("import os\nfrom pydantic import BaseModel\n", rule) == set()
    )


def test_offending_roots_denylist_flags_only_forbidden() -> None:
    """A rule with no allowed set flags only its forbidden roots, leaving the rest free."""
    rule = BoundaryRule(package="x", forbidden=frozenset({"prefect"}), probe="")
    assert _offending_roots("import prefect\nimport pandas\n", rule) == {"prefect"}
    assert _offending_roots("import pandas\n", rule) == set()


def test_boundary_rule_rejects_overlapping_allowed_and_forbidden() -> None:
    """A root in both sets is a contradiction the two guards would read opposite ways."""
    with pytest.raises(AssertionError):
        BoundaryRule(
            package="x",
            forbidden=frozenset({"os"}),
            probe="",
            allowed=frozenset({"os"}),
        )
