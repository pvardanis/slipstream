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

    :param package: the importable package the rule governs (e.g. ``slipstream_bench.contract``).
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
        package="slipstream_bench.orchestration",
        # The orchestration worker drives the sweep from the control point on Prefect and
        # boto3, depending inward on the contract kernel and the report member (ADR-0017).
        # The terminal render task calls report's plotters to draw the run's ceiling and
        # cliff plots and embed them on the parent run page (ADR-0018), so orchestration
        # pulls report's plotting stack (pandas/seaborn/matplotlib) through that one task —
        # the worker image ships it for this reason. No root is forbidden: the executor's
        # distinctive root (prometheus_client) cannot guard the no-orchestration->executor
        # edge here, since Prefect pulls it transitively; that edge is held instead by the
        # lockfile and the image build — the executor is not an orchestration dependency, so
        # --package orchestration never installs its source. orchestration shares the
        # slipstream_bench namespace with its siblings, so that root cannot be forbidden;
        # allowed holds the AST guard to the closed set orchestration legitimately imports —
        # Prefect, boto3, the report sibling, and the contract kernel.
        forbidden=frozenset(),
        allowed=frozenset(sys.stdlib_module_names)
        | {
            "slipstream_bench",
            "boto3",
            "botocore",
            "prefect",
            "prefect_aws",
            "yaml",
            "typer",
        },
        probe=(
            "import slipstream_bench.orchestration.cli as cli\n"
            "names = [command.name for command in cli.app.registered_commands]\n"
            "assert 'point-sweep' in names, names\n"
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
        package="slipstream_bench.report",
        # The report member owns the plotting stack and depends inward on the contract
        # kernel alone (ADR-0017): never Prefect, never the executor, never orchestration.
        # forbidden names the roots the subprocess probe blocks — prefect catches a stray
        # orchestration import (it pulls prefect transitively) and prometheus_client a
        # stray executor import (its distinctive root); report imports neither itself, so
        # blocking them cannot break report's own load. pandas/seaborn/matplotlib are
        # report's own stack, so they stay off forbidden and on allowed. report shares the
        # slipstream_bench namespace with its siblings, so that root cannot be forbidden;
        # allowed holds the AST guard to the closed set report legitimately imports.
        forbidden=frozenset(
            {
                "prefect",
                "prefect_aws",
                "boto3",
                "prometheus_client",
            }
        ),
        allowed=frozenset(sys.stdlib_module_names)
        | {
            "slipstream_bench",
            "matplotlib",
            "pandas",
            "seaborn",
            "typer",
        },
        probe=(
            "import slipstream_bench.report.cli as cli\n"
            "names = [command.name for command in cli.app.registered_commands]\n"
            "assert 'chart' in names, names\n"
        ),
    ),
    BoundaryRule(
        package="slipstream_bench.contract",
        # The kernel imports no framework and no other workspace member: it holds pure
        # data and parse on pydantic/PyYAML/stdlib alone (ADR-0017). forbidden names the
        # frameworks the subprocess probe blocks; allowed is the closed set the AST guard
        # holds the source to, so a new stray dep (numpy, requests) fails even though it is
        # on no denylist. The kernel shares the slipstream_bench namespace with the other
        # members, so that root cannot be forbidden; allowed admitting only its own
        # slipstream_bench.contract subtree keeps the kernel from reaching a sibling.
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
        | {"slipstream_bench", "pydantic", "yaml"},
        probe=(
            "import slipstream_bench.contract as contract\n"
            "assert contract.CellConfig is not None\n"
        ),
    ),
)
