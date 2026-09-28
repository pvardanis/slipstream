"""The ``slipstream-orchestrate`` entry guards the orchestration extra in one place.

ADR-0012 §Amendment: Prefect and boto3 ship only in the ``orchestration`` extra, and the
console script is installed by a bare ``.`` too. The entry checks the extra is present
before importing the Prefect-pulling CLI, so a bare install fails with one actionable
message naming the missing distributions, not a raw ``ImportError`` from deep inside a
module.
"""

import importlib.machinery
import importlib.util

import pytest

from slipstream_bench.orchestration.__main__ import _require_orchestration_extra


def test_require_extra_passes_when_all_modules_are_present() -> None:
    _require_orchestration_extra()


def test_require_extra_names_the_missing_module_and_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(
        name: str, package: str | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if name == "boto3":
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)

    with pytest.raises(SystemExit, match="boto3.*orchestration"):
        _require_orchestration_extra()
