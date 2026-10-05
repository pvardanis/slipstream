"""Each boundary-rule package imports with its forbidden roots blocked — the whole-closure guard.

Where ``test_sweep_is_prefect_free`` checks a package's own source statically, this checks the
whole contract — the transitive import closure and any dynamic import — by running each rule's
probe in a subprocess whose forbidden roots are blocked in ``sys.modules`` and asserting the
import still succeeds. It stands in for an environment where a framework the image does not
ship, or another workspace member the package must not depend on, was never installed
(ADR-0012 §Amendment, ADR-0017).
"""

import subprocess
import sys
import textwrap

import pytest

from tests.boundary_rules import BOUNDARY_RULES, BoundaryRule


def _block(forbidden: frozenset[str]) -> str:
    """Build a prelude that blocks each forbidden root so a later import of it raises."""
    blocked = "".join(f"sys.modules[{root!r}] = None\n" for root in sorted(forbidden))
    return "import sys\n" + blocked


def _run_with_forbidden_blocked(rule: BoundaryRule) -> subprocess.CompletedProcess[str]:
    """Run a rule's probe in a subprocess with its forbidden roots blocked."""
    return subprocess.run(
        [sys.executable, "-c", _block(rule.forbidden) + textwrap.dedent(rule.probe)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("rule", BOUNDARY_RULES, ids=lambda rule: rule.package)
def test_package_imports_without_forbidden(rule: BoundaryRule) -> None:
    result = _run_with_forbidden_blocked(rule)

    assert result.returncode == 0, (
        f"{rule.package} must import with {sorted(rule.forbidden)} blocked, but it "
        f"failed:\n{result.stderr}"
    )
