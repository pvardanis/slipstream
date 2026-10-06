"""The workspace import boundaries both guards enforce, declared as one rule table.

Each rule names an importable package, the top-level import roots it must never pull —
a framework its image does not ship, or another workspace member it must not depend on —
and a probe that loads it. A rule may also name the only roots it is allowed to pull, for
a package whose contract is a closed set (the kernel: stdlib, pydantic, PyYAML) rather than
an open set minus a few. The AST guard (``test_sweep_is_prefect_free``) walks each package's
own source and fails on an import outside the rule's boundary; the subprocess guard
(``test_import_contract``) runs each probe in an interpreter with the forbidden roots blocked
and asserts the import still succeeds. Both read this one table, so a new boundary is declared
in a single place rather than duplicated across the two checks.
"""

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class BoundaryRule:
    """One package's import boundary: the roots it may not pull, optionally the only roots
    it may pull, and a load probe.

    :param package: the importable package the rule governs (e.g. ``slipstream_bench.sweep``).
    :param forbidden: the top-level import roots the package must never pull — a framework
        its image does not ship, or another workspace member. Enforced by both guards.
    :param probe: a Python snippet that imports the package and asserts it loaded, run in a
        subprocess with every ``forbidden`` root blocked in ``sys.modules``.
    :param allowed: when set, the only top-level roots the package's own source may import —
        the AST guard fails on any root outside it, enforcing a closed contract faithfully
        rather than a denylist's "none of these few". The subprocess guard still uses
        ``forbidden``.
    """

    package: str
    forbidden: frozenset[str]
    probe: str
    allowed: frozenset[str] | None = None

    def __post_init__(self) -> None:
        """Reject a rule whose two guards would disagree about the same root.

        A root in both ``allowed`` and ``forbidden`` would pass the AST guard (inside the
        allowlist) yet be blocked by the subprocess guard — a contradiction, not a boundary.
        """
        if self.allowed is not None:
            overlap = self.allowed & self.forbidden
            assert not overlap, (
                f"{self.package}: {sorted(overlap)} is both allowed and forbidden — "
                f"the AST and subprocess guards would disagree"
            )


BOUNDARY_RULES: tuple[BoundaryRule, ...] = (
    BoundaryRule(
        package="slipstream_bench.sweep",
        forbidden=frozenset({"prefect", "prefect_aws"}),
        probe=(
            "import slipstream_bench.sweep.cli as cli\n"
            "names = [command.name for command in cli.app.registered_commands]\n"
            "assert 'aggregate-sweep' in names, names\n"
        ),
    ),
    BoundaryRule(
        package="slipstream_bench.executor",
        # The bench executor the image installs depends inward on the contract kernel
        # alone (ADR-0017): never Prefect or the plotting stack the image does not ship.
        # forbidden names the frameworks the subprocess probe blocks — blocking them also
        # catches a stray import of the report or orchestration sibling, since each pulls a
        # blocked framework (pandas/seaborn, Prefect) transitively and the probe would fail.
        # The executor shares the slipstream_bench namespace with those siblings, so that
        # root cannot be forbidden outright; allowed holds the AST guard to the closed set
        # the executor legitimately imports, so a new stray dep fails even off the denylist.
        forbidden=frozenset(
            {
                "prefect",
                "prefect_aws",
                "boto3",
                "pandas",
                "seaborn",
                "matplotlib",
            }
        ),
        allowed=frozenset(sys.stdlib_module_names)
        | {
            "slipstream",
            "slipstream_bench",
            "pydantic",
            "yaml",
            "typer",
            "prometheus_client",
        },
        probe=(
            "import slipstream_bench.executor.cli as cli\n"
            "names = [command.name for command in cli.app.registered_commands]\n"
            "assert 'load-cell' in names, names\n"
        ),
    ),
    BoundaryRule(
        package="slipstream.contract",
        # The kernel imports no framework and no other workspace member: it holds pure
        # data and parse on pydantic/PyYAML/stdlib alone (ADR-0017). forbidden names the
        # frameworks the subprocess probe blocks; allowed is the closed set the AST guard
        # holds the source to, so a new stray dep (numpy, requests) fails even though it is
        # on no denylist.
        forbidden=frozenset(
            {
                "prefect",
                "prefect_aws",
                "boto3",
                "pandas",
                "seaborn",
                "matplotlib",
                "slipstream_bench",
            }
        ),
        allowed=frozenset(sys.stdlib_module_names) | {"slipstream", "pydantic", "yaml"},
        probe=(
            "import slipstream.contract as contract\n"
            "assert contract.CellConfig is not None\n"
        ),
    ),
)
