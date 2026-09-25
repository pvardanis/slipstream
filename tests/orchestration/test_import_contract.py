"""The Prefect-free import contract, exercised in a Prefect-free interpreter.

ADR-0012 §Amendment splits the package so the bench-client image installs ``.`` with
no Prefect: its only import path, ``load-sweep`` → ``sweep/``, must import cleanly with
neither ``prefect`` nor ``prefect_aws`` present. The AST guard in
``test_sweep_is_prefect_free`` checks ``sweep/``'s own source statically; these tests
check the whole contract — the transitive import closure and any dynamic import — by
running a subprocess whose ``prefect`` and ``prefect_aws`` are blocked in
``sys.modules`` and asserting the import still succeeds. The mirror case is also pinned:
``orchestration.storage`` must import Prefect-free too (its Prefect imports are lazy),
so hoisting one back to module scope, which the Prefect-present CI would not catch,
fails here instead.
"""

import subprocess
import sys
import textwrap

# Blocking a name in sys.modules makes any later ``import`` of it raise, standing in
# for an environment where the orchestration extra was never installed.
_BLOCK_PREFECT = """
import sys
sys.modules["prefect"] = None
sys.modules["prefect_aws"] = None
"""


def _run_prefect_free(body: str) -> subprocess.CompletedProcess[str]:
    """Run ``body`` in a subprocess with prefect and prefect_aws blocked."""
    return subprocess.run(
        [sys.executable, "-c", _BLOCK_PREFECT + textwrap.dedent(body)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_sweep_and_load_sweep_import_without_prefect() -> None:
    result = _run_prefect_free(
        """
        import slipstream_bench.sweep.cli as cli
        names = [command.name for command in cli.app.registered_commands]
        assert "load-sweep" in names, names
        """
    )

    assert result.returncode == 0, (
        "the bench-client import path must load with no prefect present, but "
        f"importing slipstream_bench.sweep failed:\n{result.stderr}"
    )


def test_orchestration_storage_imports_without_prefect() -> None:
    result = _run_prefect_free(
        """
        import slipstream_bench.orchestration.storage as storage
        assert hasattr(storage, "result_storage")
        """
    )

    assert result.returncode == 0, (
        "orchestration.storage must import with no prefect present (its prefect "
        f"imports are lazy), but importing it failed:\n{result.stderr}"
    )
