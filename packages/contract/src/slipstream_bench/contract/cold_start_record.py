"""One cold start's breakdown: its pod-created → Ready total split into phases (ADR-0030).

The cold-start-breakdown flow writes one record per run under the results bucket's
``cold-start`` root, and the report reads only these records. Each phase is a duration
in seconds bounded by named cluster markers; the unattributed remainder is the total
minus the phases, so the phases always add up to the cold start. It can read slightly
negative, since the markers carry up to about a second of rounding.

Like the KV cache A/B's record, the module is kept out of the kernel's package surface:
only the cold-start flow and the report need it.
"""

from dataclasses import dataclass


class ColdStartRecordError(Exception):
    """A cold-start breakdown whose run number or durations cannot be a cold start."""


@dataclass(frozen=True)
class ColdStartBreakdown:
    """One run's cold start and its cluster-side phases, in seconds.

    :param run: the run's 1-based number within its measurement.
    :param total_s: pod created → pod Ready.
    :param capacity_wait_s: pod created → the hosting NodeClaim's ``Launched``.
    :param node_launch_s: the NodeClaim's ``Launched`` → its ``Initialized``.
    :param image_pull_s: kubelet's ``Pulling`` → ``Pulled`` for the engine container.
    """

    run: int
    total_s: float
    capacity_wait_s: float
    node_launch_s: float
    image_pull_s: float

    def __post_init__(self) -> None:
        """Reject a run number below one or a negative total or phase.

        A negative phase means its end marker came before its start marker, so the
        markers were misread rather than the phase taking no time.

        :raise ColdStartRecordError: when the run number or a duration is impossible.
        """
        if self.run < 1:
            raise ColdStartRecordError(
                f"cold-start run {self.run} is not a 1-based run number"
            )
        for name in ("total_s", "capacity_wait_s", "node_launch_s", "image_pull_s"):
            seconds = getattr(self, name)
            if seconds < 0:
                raise ColdStartRecordError(
                    f"cold-start run {self.run}'s {name} is {seconds}: its end marker "
                    f"is before its start marker"
                )

    @property
    def unattributed_s(self) -> float:
        """The part of the total no phase covers."""
        return self.total_s - (
            self.capacity_wait_s + self.node_launch_s + self.image_pull_s
        )

    def to_record(self) -> dict[str, int | float]:
        """Return the record the results bucket stores and the report reads.

        :return: the run number, the total, each phase and the unattributed remainder.
        """
        return {
            "run": self.run,
            "total_s": self.total_s,
            "capacity_wait_s": self.capacity_wait_s,
            "node_launch_s": self.node_launch_s,
            "image_pull_s": self.image_pull_s,
            "unattributed_s": self.unattributed_s,
        }
