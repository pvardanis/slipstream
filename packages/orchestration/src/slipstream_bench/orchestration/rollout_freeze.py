"""The rollout freeze: hold Argo CD off syncing the workloads while a run measures them.

ADR-0029: a merge to main that Argo CD synced mid-run would roll the vLLM replica or the
worker under a knob sweep or a KV cache A/B and corrupt what it measures. So a parent
flow holds a rollout freeze for its run: an Argo CD deny sync window on the ``workloads``
AppProject, active from the current minute for the run's timeout, that denies manual syncs
too. Argo CD queues what merged meanwhile and syncs it once the window is gone.

The freeze is a port (:class:`RolloutFreeze`) the flow reaches Argo CD through, the way it
reaches the cluster through :data:`~slipstream_bench.orchestration.cluster.Kubectl`;
:class:`AppProjectFreeze` is its adapter, which edits the AppProject's
``spec.syncWindows`` over ``kubectl`` under a grant (``terraform/eks/worker_rollout_freeze.tf``)
of get and patch on that one AppProject. Each window names its run in its description, so a
release removes only its own run's window and leaves the others in place.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from slipstream_bench.orchestration.cluster import Kubectl

# The Argo CD namespace and the AppProject the workloads' Applications belong to
# (terraform/eks/argocd.tf); the worker's RBAC grants get and patch on that one project.
_NAMESPACE = "argocd"
_PROJECT = "workloads"

# The description a run's window carries, which a release matches its own window by.
_DESCRIPTION = "slipstream rollout freeze: run={holder}"


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
    :param namespace: the namespace the AppProject lives in.
    :param project: the AppProject whose Applications the freeze holds.
    """

    kubectl: Kubectl
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    namespace: str = _NAMESPACE
    project: str = _PROJECT

    def acquire(self, holder: str, duration_s: int) -> None:
        """Add ``holder``'s deny window, active from the current UTC minute.

        Argo CD has no one-off window, only a cron schedule and a duration, so the
        schedule is pinned to the current minute, day and month: the window is active
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
        """
        project = self._get_project()
        windows = _get_other_windows(project, holder)
        if len(windows) == len(_get_windows(project)):
            return
        self._patch_windows(project, windows)

    def _get_project(self) -> dict[str, Any]:
        return json.loads(
            self.kubectl(
                [
                    "-n",
                    self.namespace,
                    "get",
                    "appproject",
                    self.project,
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
                self.namespace,
                "patch",
                "appproject",
                self.project,
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
