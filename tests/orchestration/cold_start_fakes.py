"""A ``Kubectl`` fake replaying one recorded true-zero cold start (tests/fixtures/cold-start).

Answers a ``get`` of pods, events or NodeClaims with the recorded objects, as ``kubectl``
returns them with ``-o json``: the pod and the NodeClaim inside a list, the namespace's
events as captured. A test edits the loaded objects to drop a marker before replaying,
or sets ``pods`` to list other than the one recorded pod.
"""

import json
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from slipstream_bench.orchestration.cluster import KubectlError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "cold-start"


def load_fixture(name: str) -> Any:
    """Load one recorded object from the cold-start fixtures."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RecordedCluster:
    """Replay the recorded pod, events and NodeClaim, and record each argv issued."""

    def __init__(self) -> None:
        self.pod: dict[str, Any] = load_fixture("pod.json")
        self.pods: list[dict[str, Any]] | None = None
        self.events: dict[str, Any] = load_fixture("events.json")
        self.nodeclaims: list[dict[str, Any]] = [load_fixture("nodeclaim.json")]
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        self.calls.append(list(args))
        if "get" not in args:
            raise AssertionError(f"unexpected kubectl call: {list(args)}")
        resource = args[args.index("get") + 1]
        return json.dumps(deepcopy(self._get(resource)))

    def _get(self, resource: str) -> Any:
        if resource == "pods":
            pods = [self.pod] if self.pods is None else self.pods
            return {"kind": "List", "items": pods}
        if resource == "events":
            return self.events
        if resource == "nodeclaims":
            return {"kind": "List", "items": self.nodeclaims}
        raise AssertionError(f"unexpected kubectl get {resource}")


class CyclingCluster(RecordedCluster):
    """Replay the recorded cold start behind a replica the flow scales to zero and back.

    A ``patch`` of the Deployment sets its replicas. Once scaled to zero, the warm GPU
    NodeClaim outlives ``drain_polls`` polls of the GPU pool's NodeClaims before Karpenter
    removes it; scaled back to one, the recorded NodeClaim hosts the new pod. A rollout
    wait returns once the replica is Ready, or fails as ``kubectl`` does on its timeout
    when ``ready`` is unset. Each step is appended to ``steps``, which a test may share
    with a recording freeze to place them among its acquire and release.
    """

    def __init__(
        self,
        *,
        drain_polls: int = 1,
        ready: bool = True,
        steps: list[str] | None = None,
    ) -> None:
        super().__init__()
        warm = deepcopy(self.nodeclaims[0])
        warm["metadata"]["name"] = "gpu-warm"
        self.gpu_nodeclaims: list[dict[str, Any]] = [warm]
        self.replicas = 1
        self.drain_polls = drain_polls
        self.ready = ready
        self.steps = [] if steps is None else steps

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str:
        if "patch" in args:
            self.calls.append(list(args))
            self._scale(json.loads(args[args.index("-p") + 1])["spec"]["replicas"])
            return ""
        if "rollout" in args:
            self.calls.append(list(args))
            self.steps.append("wait ready")
            if not self.ready:
                raise KubectlError(
                    f"kubectl {' '.join(args)} exited 1: error: timed out waiting for "
                    f"the condition"
                )
            return 'deployment "vllm-gpu" successfully rolled out\n'
        if "-l" in args and "nodeclaims" in args:
            self.calls.append(list(args))
            self.steps.append("poll gpu nodeclaims")
            return json.dumps({"kind": "List", "items": self._poll_gpu_nodeclaims()})
        self.steps.append(f"get {args[args.index('get') + 1]}")
        return super().__call__(args, stdin)

    def _scale(self, replicas: int) -> None:
        self.steps.append(f"scale {replicas}")
        self.replicas = replicas
        if replicas == 1:
            self.gpu_nodeclaims = deepcopy(self.nodeclaims)

    def _poll_gpu_nodeclaims(self) -> list[dict[str, Any]]:
        if self.replicas == 0 and self.gpu_nodeclaims and self.drain_polls == 0:
            self.gpu_nodeclaims = []
        elif self.replicas == 0:
            self.drain_polls -= 1
        return deepcopy(self.gpu_nodeclaims)


class FakeClock:
    """A monotonic clock a poll's sleeps advance, so a timeout elapses instantly."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
