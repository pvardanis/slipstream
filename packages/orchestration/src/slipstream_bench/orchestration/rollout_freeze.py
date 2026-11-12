"""The rollout freeze: hold Argo CD off syncing the workloads while a run measures them.

ADR-0029: a merge to main that Argo CD synced mid-run would roll the vLLM replica or the
worker under a knob sweep or a KV cache A/B and corrupt what it measures. So a parent
flow holds a rollout freeze for its run: an Argo CD deny sync window on the ``workloads``
AppProject, active from the current minute for the run's timeout. The window blocks
automated syncs and self-heal but lets a manual sync through, so an operator can still roll
out by hand. Argo CD queues what merged meanwhile and syncs it once the window is gone.

The freeze is a port (:class:`RolloutFreeze`) the flow reaches Argo CD through, the way it
reaches the cluster through :data:`~slipstream_bench.orchestration.cluster.Kubectl`;
:class:`AppProjectFreeze` is its adapter, which edits the AppProject's
``spec.syncWindows`` over ``kubectl`` under a grant (``terraform/eks/worker_rollout_freeze.tf``)
of get and patch on that one AppProject. Each window names its run in its description, so a
release removes only its own run's window and leaves the others in place.
:func:`hold_rollout_freeze` holds a freeze around a run's work and releases it on exit.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from slipstream_bench.orchestration.cluster import Kubectl, KubectlError

# The Argo CD namespace and the AppProject the workloads' Applications belong to
# (terraform/eks/argocd.tf); the worker's RBAC grants get and patch on that one project.
_NAMESPACE = "argocd"
_PROJECT = "workloads"

# The command an operator removes a window left behind with.
_EDIT_COMMAND = f"kubectl -n {_NAMESPACE} edit appproject {_PROJECT}"

# The description a run's window carries, which a release matches its own window by.
_DESCRIPTION = "slipstream rollout freeze: run={holder}"

# The line a parent flow's crash, cancellation or timeout hook logs on the run once it has
# released the run's freeze; the session's error names it as what to look for.
HOOK_RELEASED_LINE = "released run %s's rollout freeze"

_LOGGER = logging.getLogger(__name__)


class RolloutFreeze(Protocol):
    """Hold Argo CD off syncing the workloads for a run, then let it sync again."""

    def acquire(self, holder: str, duration_s: int) -> None:
        """Freeze rollouts for ``duration_s`` seconds from now, on behalf of ``holder``."""
        ...

    def release(self, holder: str) -> None:
        """Lift ``holder``'s freeze; a holder with no freeze is left as it is."""
        ...


@dataclass(frozen=True)
class AppProjectFreeze:
    """The :class:`RolloutFreeze` an Argo CD deny sync window on one AppProject holds.

    :param kubectl: the transport the AppProject is read and patched through.
    :param now: the clock the window's start is pinned to.
    """

    kubectl: Kubectl
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def acquire(self, holder: str, duration_s: int) -> None:
        """Add ``holder``'s deny window, active from the current UTC minute.

        Argo CD has no one-off window, only a cron schedule and a duration, so the
        schedule is pinned to the current minute, hour, day and month: the window is active
        at once and lasts ``duration_s``. A window the holder left behind (a run resumed
        after its process was killed before it released) is replaced, not duplicated.

        :param holder: the run the window is held for, named in its description.
        :param duration_s: how long the window lasts, in seconds.
        """
        start = self.now().astimezone(UTC)
        window = {
            "kind": "deny",
            "schedule": f"{start.minute} {start.hour} {start.day} {start.month} *",
            "duration": f"{duration_s}s",
            "applications": ["*"],
            "manualSync": True,
            "timeZone": "UTC",
            "description": _DESCRIPTION.format(holder=holder),
        }
        project = self._get_project()
        self._patch_windows(project, [*_get_other_windows(project, holder), window])

    def release(self, holder: str) -> None:
        """Remove ``holder``'s window, leaving the project's other windows in place.

        A project with no window for ``holder`` (one its run's session already released
        before a hook released it again) is left unpatched.

        :param holder: the run whose window is removed.
        :raise KubectlError: when the AppProject cannot be read or patched, noted with the
            window left behind and the command that removes it.
        """
        try:
            project = self._get_project()
            windows = _get_other_windows(project, holder)
            if len(windows) == len(_get_windows(project)):
                return
            self._patch_windows(project, windows)
        except KubectlError as error:
            error.add_note(
                f'the "{_DESCRIPTION.format(holder=holder)}" window freezes the '
                "workloads until it lapses; remove it with "
                f"`{_EDIT_COMMAND}`"
            )
            raise

    def _get_project(self) -> dict[str, Any]:
        return json.loads(
            self.kubectl(
                [
                    "-n",
                    _NAMESPACE,
                    "get",
                    "appproject",
                    _PROJECT,
                    "-o",
                    "json",
                ],
                None,
            )
        )

    def _patch_windows(
        self, project: dict[str, Any], windows: list[dict[str, Any]]
    ) -> None:
        patch = {
            "metadata": {"resourceVersion": project["metadata"]["resourceVersion"]},
            "spec": {"syncWindows": windows},
        }
        self.kubectl(
            [
                "-n",
                _NAMESPACE,
                "patch",
                "appproject",
                _PROJECT,
                "--type=merge",
                "-p",
                json.dumps(patch),
            ],
            None,
        )


def _get_other_windows(project: dict[str, Any], holder: str) -> list[dict[str, Any]]:
    """Return the project's sync windows other than ``holder``'s, in their order."""
    description = _DESCRIPTION.format(holder=holder)
    return [
        window
        for window in _get_windows(project)
        if window.get("description") != description
    ]


def _get_windows(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the project's sync windows; a project with none omits the field."""
    return project["spec"].get("syncWindows") or []


@contextmanager
def hold_rollout_freeze(
    freeze: RolloutFreeze, *, holder: str, duration_s: int
) -> Iterator[None]:
    """Acquire ``holder``'s rollout freeze, yield, and release it on exit.

    The release follows a body that returns or raises an ``Exception``; when the body has
    raised, a release that fails too is logged and the body's error is the one raised. A
    run being killed (a ``BaseException`` that is not an ``Exception``, such as Prefect's
    termination signal, or the cancellation its timeout raises) skips it and leaves it to
    the flow's crash, cancellation or timeout hooks, where it runs after the bench
    endpoint's destroy hook has returned or failed: a
    rollout Argo CD started on a release here could restart the worker under that
    destroy. The window's own duration lifts the freeze if no hook answers.

    :param freeze: the freeze to acquire and release.
    :param holder: the run the freeze is held for.
    :param duration_s: how long the freeze lasts unreleased, in seconds — the run's
        timeout.
    :return: a context manager holding the freeze for its body.
    """
    freeze.acquire(holder, duration_s)
    _LOGGER.info("rollout freeze held for run %s (%ds)", holder, duration_s)
    try:
        yield
    except Exception:
        # The run fails for what went wrong in it, so a release that fails as well is
        # logged beside that failure rather than raised over it.
        try:
            _release(freeze, holder)
        except Exception:
            _LOGGER.exception("rollout freeze release for run %s failed", holder)
        raise
    except BaseException as error:
        # Logged as an error naming the recovery, since a signal no hook answers leaves
        # the workloads frozen until the window lapses.
        _LOGGER.error(
            "rollout freeze not released on %s; it is left for the run's crash, "
            'cancellation or timeout hook. If the run page shows no "%s" line from that '
            'hook, remove the "%s" window with `%s`',
            type(error).__name__,
            HOOK_RELEASED_LINE % "<run-id>",
            _DESCRIPTION.format(holder=holder),
            _EDIT_COMMAND,
        )
        raise
    _release(freeze, holder)


def _release(freeze: RolloutFreeze, holder: str) -> None:
    freeze.release(holder)
    _LOGGER.info("rollout freeze released for run %s", holder)
