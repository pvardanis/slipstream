"""The workspace import boundaries both guards enforce, declared as one rule table.

Each rule names an importable package, the top-level import roots it must never pull —
a framework its image does not ship, or another workspace member it must not depend on —
and a probe that loads it. The AST guard (``test_sweep_is_prefect_free``) walks each
package's own source for a forbidden ``import``; the subprocess guard
(``test_import_contract``) runs each probe in an interpreter with those roots blocked and
asserts the import still succeeds. Both read this one table, so a new boundary is declared
in a single place rather than duplicated across the two checks.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BoundaryRule:
    """One package's import boundary: the roots it must not pull, and a load probe.

    :param package: the importable package the rule governs (e.g. ``slipstream_bench.sweep``).
    :param forbidden: the top-level import roots the package must never pull — a framework
        its image does not ship, or another workspace member.
    :param probe: a Python snippet that imports the package and asserts it loaded, run in a
        subprocess with every ``forbidden`` root blocked in ``sys.modules``.
    """

    package: str
    forbidden: frozenset[str]
    probe: str


BOUNDARY_RULES: tuple[BoundaryRule, ...] = (
    BoundaryRule(
        package="slipstream_bench.sweep",
        forbidden=frozenset({"prefect", "prefect_aws"}),
        probe=(
            "import slipstream_bench.sweep.cli as cli\n"
            "names = [command.name for command in cli.app.registered_commands]\n"
            "assert 'load-sweep' in names, names\n"
        ),
    ),
    BoundaryRule(
        package="slipstream.contract",
        # The kernel imports no framework and no other workspace member: it holds pure
        # data and parse on pydantic/PyYAML/stdlib alone (ADR-0017).
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
        probe=(
            "import slipstream.contract as contract\n"
            "assert contract.CellConfig is not None\n"
        ),
    ),
)
