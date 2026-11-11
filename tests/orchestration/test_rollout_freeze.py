"""The rollout freeze: an Argo CD deny sync window held on an AppProject for a run.

Exercises the AppProject freeze against a fake kubectl that serves the AppProject's JSON
and applies merge patches to it, rejecting one whose resourceVersion is stale as the API
server does — no cluster. Asserts the window a freeze adds (deny, active from the current
UTC minute, lasting the run's timeout, manual syncs let through), that it keeps every other
window on the project, and that a release removes only its own run's window. Drives
the session that holds a freeze around a run's work against a recording freeze: it
releases on any exit but a killed run's, which it leaves to the run's hook.
"""

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from prefect.exceptions import TerminationSignal

from slipstream_bench.orchestration.cluster import KubectlError
from slipstream_bench.orchestration.rollout_freeze import (
    AppProjectFreeze,
    hold_rollout_freeze,
)
from tests.orchestration.rollout_freeze_fakes import RecordingFreeze

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
        self.patch_error: Exception | None = None
        self.written_after_get = False

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        if list(args) == _GET:
            served = json.dumps(
                {
                    "metadata": {
                        "name": "workloads",
                        "resourceVersion": str(self.resource_version),
                    },
                    "spec": self.spec,
                }
            )
            if self.written_after_get:
                self.resource_version += 1
            return served
        assert list(args[:-1]) == _PATCH, f"unexpected kubectl {list(args)}"
        if self.patch_error is not None:
            raise self.patch_error
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


def test_acquire_fails_rather_than_overwrite_a_window_written_since_its_read() -> None:
    # A knob sweep and a KV cache A/B acquiring at once: the later patch carries a stale
    # version, so it fails instead of dropping the other run's window.
    project = _FakeAppProject([_holder_window("kv-ab-7")])
    project.written_after_get = True

    with pytest.raises(KubectlError, match="modified"):
        _freeze(project).acquire("knob-20261111", 68400)

    assert project.spec["syncWindows"] == [_holder_window("kv-ab-7")]


def test_acquire_pins_the_schedule_to_the_utc_time_of_a_local_clock() -> None:
    # 00:30 on 12 Nov in UTC+2 is 22:30 on 11 Nov in UTC, the window's time zone.
    project = _FakeAppProject()
    local = datetime(2026, 11, 12, 0, 30, tzinfo=timezone(timedelta(hours=2)))

    AppProjectFreeze(kubectl=project, now=lambda: local).acquire("knob-20261111", 60)

    assert project.spec["syncWindows"][0]["schedule"] == "30 22 11 11 *"


def test_a_failed_release_notes_the_window_left_and_how_to_remove_it() -> None:
    # The window freezes the workloads until it lapses, so the error names it and the
    # command that removes it.
    project = _FakeAppProject([_holder_window("knob-20261111")])
    project.patch_error = KubectlError("kubectl patch exited 1: forbidden")

    with pytest.raises(KubectlError) as raised:
        _freeze(project).release("knob-20261111")

    [note] = raised.value.__notes__
    assert "slipstream rollout freeze: run=knob-20261111" in note
    assert "kubectl -n argocd edit appproject workloads" in note


def test_session_holds_the_freeze_around_its_body() -> None:
    freeze = RecordingFreeze()

    with hold_rollout_freeze(freeze, holder="knob-20261111", duration_s=68400):
        assert freeze.calls == [("acquire", "knob-20261111", "68400")]

    assert freeze.calls[-1] == ("release", "knob-20261111")


def test_session_releases_the_freeze_when_its_body_fails() -> None:
    freeze = RecordingFreeze()

    with (
        pytest.raises(ValueError, match="point failed"),
        hold_rollout_freeze(freeze, holder="knob-20261111", duration_s=68400),
    ):
        raise ValueError("point failed")

    assert freeze.calls[-1] == ("release", "knob-20261111")


class _FailingReleaseFreeze(RecordingFreeze):
    """A freeze whose release fails, as a forbidden or conflicting patch does."""

    def release(self, holder: str) -> None:
        super().release(holder)
        raise KubectlError("kubectl patch exited 1: forbidden")


def test_session_keeps_the_body_s_failure_when_the_release_also_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The run fails for what went wrong in it; the release failure is logged beside it.
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    with (
        pytest.raises(ValueError, match="point failed"),
        hold_rollout_freeze(
            _FailingReleaseFreeze(), holder="knob-20261111", duration_s=68400
        ),
    ):
        raise ValueError("point failed")

    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "knob-20261111" in record.getMessage()
    assert record.exc_info is not None
    assert "forbidden" in str(record.exc_info[1])


def test_session_raises_the_release_failure_after_its_body_succeeds() -> None:
    with (
        pytest.raises(KubectlError, match="forbidden"),
        hold_rollout_freeze(
            _FailingReleaseFreeze(), holder="knob-20261111", duration_s=68400
        ),
    ):
        pass


@pytest.mark.parametrize("signal", [TerminationSignal, KeyboardInterrupt])
def test_session_leaves_the_release_to_the_hook_when_the_run_is_killed(
    signal: type[BaseException],
) -> None:
    # The hooks destroy the bench endpoint before they release the freeze, so a rollout
    # Argo CD started on a release here could restart the worker under that destroy.
    freeze = RecordingFreeze()

    with (
        pytest.raises(signal),
        hold_rollout_freeze(freeze, holder="knob-20261111", duration_s=68400),
    ):
        raise signal(*((15,) if signal is TerminationSignal else ()))

    assert ("release", "knob-20261111") not in freeze.calls


def test_session_logs_its_acquire_and_release(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    with hold_rollout_freeze(
        RecordingFreeze(), holder="knob-20261111", duration_s=68400
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
            RecordingFreeze(), holder="knob-20261111", duration_s=68400
        ),
    ):
        raise TerminationSignal(15)

    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    # Names the line the hook logs on the run once it has released, so its absence is
    # what calls for the manual removal, and the window that removal targets.
    assert "released run knob-20261111's rollout freeze" in record.getMessage()
    assert "slipstream rollout freeze: run=knob-20261111" in record.getMessage()
    assert "kubectl -n argocd edit appproject workloads" in record.getMessage()
