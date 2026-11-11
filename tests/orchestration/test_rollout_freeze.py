"""The rollout freeze: an Argo CD deny sync window held on an AppProject for a run.

Exercises the AppProject freeze against a fake kubectl that serves the AppProject's JSON
and applies merge patches to it, rejecting one whose resourceVersion is stale as the API
server does — no cluster. Asserts the window a freeze adds (deny, active from the current
UTC minute, lasting the run's timeout, manual syncs denied too), that it keeps every other
window on the project, and that a release removes only its own run's window. Drives
the session that holds a freeze around a run's work against a recording freeze: it
releases on any exit but a killed run's, which it leaves to the run's hook.
"""

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from prefect.exceptions import TerminationSignal

from slipstream_bench.orchestration.cluster import KubectlError
from slipstream_bench.orchestration.rollout_freeze import (
    AppProjectFreeze,
    hold_rollout_freeze,
)

_LOGGER_NAME = "slipstream_bench.orchestration.rollout_freeze"
_NOW = datetime(2026, 11, 11, 14, 7, 33, tzinfo=UTC)
_GET = ["-n", "argocd", "get", "appproject", "workloads", "-o", "json"]
_PATCH = ["-n", "argocd", "patch", "appproject", "workloads", "--type=merge", "-p"]


class _FakeAppProject:
    """Serve one AppProject over kubectl ``get`` and apply ``patch --type=merge`` to it.

    A patch carrying a resourceVersion other than the project's current one is rejected
    the way the API server rejects it, and each accepted patch bumps the version.
    """

    def __init__(self, windows: list[dict[str, Any]] | None = None) -> None:
        self.spec: dict[str, Any] = {} if windows is None else {"syncWindows": windows}
        self.resource_version = 41
        self.patches: list[dict[str, Any]] = []

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        if list(args) == _GET:
            return json.dumps(
                {
                    "metadata": {
                        "name": "workloads",
                        "resourceVersion": str(self.resource_version),
                    },
                    "spec": self.spec,
                }
            )
        assert list(args[:-1]) == _PATCH, f"unexpected kubectl {list(args)}"
        patch = json.loads(args[-1])
        if patch["metadata"]["resourceVersion"] != str(self.resource_version):
            raise KubectlError(
                "Operation cannot be fulfilled: the object has been modified"
            )
        self.spec["syncWindows"] = patch["spec"]["syncWindows"]
        self.resource_version += 1
        self.patches.append(patch)
        return ""


def _freeze(project: _FakeAppProject) -> AppProjectFreeze:
    return AppProjectFreeze(kubectl=project, now=lambda: _NOW)


def test_acquire_adds_a_deny_window_active_now_for_the_duration() -> None:
    project = _FakeAppProject()

    _freeze(project).acquire("knob-20261111", 68400)

    assert project.spec["syncWindows"] == [
        {
            "kind": "deny",
            "schedule": "7 14 11 11 *",
            "duration": "68400s",
            "applications": ["*"],
            "manualSync": True,
            "timeZone": "UTC",
            "description": "slipstream rollout freeze: run=knob-20261111",
        }
    ]


_OTHER_WINDOW = {
    "kind": "allow",
    "schedule": "0 22 * * *",
    "duration": "1h",
    "applications": ["*"],
}


def _holder_window(holder: str) -> dict[str, Any]:
    return {
        "kind": "deny",
        "schedule": "0 3 1 1 *",
        "duration": "60s",
        "applications": ["*"],
        "manualSync": True,
        "timeZone": "UTC",
        "description": f"slipstream rollout freeze: run={holder}",
    }


def test_acquire_keeps_the_project_s_other_windows_and_patches_its_read_version() -> (
    None
):
    project = _FakeAppProject([_OTHER_WINDOW])

    _freeze(project).acquire("knob-20261111", 68400)

    assert project.spec["syncWindows"][0] == _OTHER_WINDOW
    assert len(project.spec["syncWindows"]) == 2
    assert project.patches[0]["metadata"] == {"resourceVersion": "41"}


def test_acquire_replaces_a_window_its_run_left_behind() -> None:
    project = _FakeAppProject([_holder_window("knob-20261111"), _OTHER_WINDOW])

    _freeze(project).acquire("knob-20261111", 68400)

    schedules = [
        window["schedule"]
        for window in project.spec["syncWindows"]
        if window.get("description") == "slipstream rollout freeze: run=knob-20261111"
    ]
    assert schedules == ["7 14 11 11 *"]
    assert _OTHER_WINDOW in project.spec["syncWindows"]


def test_release_removes_only_its_run_s_window() -> None:
    project = _FakeAppProject(
        [_OTHER_WINDOW, _holder_window("knob-20261111"), _holder_window("kv-ab-7")]
    )

    _freeze(project).release("knob-20261111")

    assert project.spec["syncWindows"] == [_OTHER_WINDOW, _holder_window("kv-ab-7")]
    assert project.patches[0]["metadata"] == {"resourceVersion": "41"}


def test_release_without_its_run_s_window_leaves_the_project_unpatched() -> None:
    project = _FakeAppProject([_OTHER_WINDOW])

    _freeze(project).release("knob-20261111")

    assert project.patches == []


class _RecordingFreeze:
    """Record each acquire and release the session makes, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def acquire(self, holder: str, duration_s: int) -> None:
        self.calls.append(("acquire", holder, str(duration_s)))

    def release(self, holder: str) -> None:
        self.calls.append(("release", holder))


def test_session_holds_the_freeze_around_its_body() -> None:
    freeze = _RecordingFreeze()

    with hold_rollout_freeze(freeze, holder="knob-20261111", duration_s=68400):
        assert freeze.calls == [("acquire", "knob-20261111", "68400")]

    assert freeze.calls[-1] == ("release", "knob-20261111")


def test_session_releases_the_freeze_when_its_body_fails() -> None:
    freeze = _RecordingFreeze()

    with (
        pytest.raises(ValueError, match="point failed"),
        hold_rollout_freeze(freeze, holder="knob-20261111", duration_s=68400),
    ):
        raise ValueError("point failed")

    assert freeze.calls[-1] == ("release", "knob-20261111")


@pytest.mark.parametrize("signal", [TerminationSignal, KeyboardInterrupt])
def test_session_leaves_the_release_to_the_hook_when_the_run_is_killed(
    signal: type[BaseException],
) -> None:
    # The hooks destroy the bench endpoint before they release the freeze, so a rollout
    # Argo CD started on a release here could restart the worker under that destroy.
    freeze = _RecordingFreeze()

    with (
        pytest.raises(signal),
        hold_rollout_freeze(freeze, holder="knob-20261111", duration_s=68400),
    ):
        raise signal(*((15,) if signal is TerminationSignal else ()))

    assert ("release", "knob-20261111") not in freeze.calls


def test_session_logs_its_acquire_and_release(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    with hold_rollout_freeze(
        _RecordingFreeze(), holder="knob-20261111", duration_s=68400
    ):
        pass

    assert [r.getMessage() for r in caplog.records if r.name == _LOGGER_NAME] == [
        "rollout freeze held for run knob-20261111 (68400s)",
        "rollout freeze released for run knob-20261111",
    ]


def test_session_logs_that_it_left_the_freeze_for_the_hook(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    with (
        pytest.raises(TerminationSignal),
        hold_rollout_freeze(
            _RecordingFreeze(), holder="knob-20261111", duration_s=68400
        ),
    ):
        raise TerminationSignal(15)

    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    # Names the line the hook logs on the run once it has released, so its absence is
    # what calls for the manual removal, and the window that removal targets.
    assert "released run knob-20261111's rollout freeze" in record.getMessage()
    assert "slipstream rollout freeze: run=knob-20261111" in record.getMessage()
    assert "kubectl -n argocd edit appproject workloads" in record.getMessage()
