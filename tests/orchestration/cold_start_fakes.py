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
