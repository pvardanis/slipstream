"""A recording rollout freeze, shared by the freeze session's and the flows' tests.

Stands in for the :class:`~slipstream_bench.orchestration.rollout_freeze.RolloutFreeze`
port: it records each acquire and release, and a test that passes its own ``events`` list
places them among the other steps it records there.
"""


class RecordingFreeze:
    """Record each acquire and release made on the freeze, in order."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.events = [] if events is None else events

    def acquire(self, holder: str, duration_s: int) -> None:
        self.calls.append(("acquire", holder, str(duration_s)))
        self.events.append("freeze acquire")

    def release(self, holder: str) -> None:
        self.calls.append(("release", holder))
        self.events.append("freeze release")
