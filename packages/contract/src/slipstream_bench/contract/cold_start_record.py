"""One cold start's breakdown: its pod-created → Ready total split into phases (ADR-0030).

The cold-start-breakdown flow writes one record per run under the results bucket's
``cold-start`` root. Each of the six phases is a duration in seconds bounded by named
cluster or engine-log markers; the unattributed remainder is the total minus the phases,
so the phases always add up to the cold start. The remainder holds scheduling and
sandbox creation, container start through the engine's imports, process start and model
init, and the API server's start. It is not checked for sign, since the markers carry up to about a second of rounding. Engine init's
torch.compile, graph capture and profiling-run durations are detail inside engine init,
not phases, so they are not subtracted from the total.

Like the KV cache A/B's record, the module is kept out of the kernel's package surface:
only the cold-start flow needs it.
"""

from dataclasses import dataclass, fields

# The six phases, in order, which with the remainder add up to the total.
PHASES = (
    "capacity_wait_s",
    "node_launch_s",
    "image_pull_s",
    "weight_download_s",
    "engine_init_s",
    "readiness_lag_s",
)
# Engine init's detail: durations inside engine init, not phases of their own.
ENGINE_INIT_DETAIL = ("torch_compile_s", "graph_capture_s", "profiling_run_s")
# Seconds are recorded to the millisecond, so the remainder carries no float noise.
SECONDS_DECIMALS = 3


class ColdStartRecordError(Exception):
    """A cold-start breakdown whose run number or durations cannot be a cold start."""


@dataclass(frozen=True, kw_only=True)
class ColdStartBreakdown:
    """One run's cold start, its six phases and engine init's detail, in seconds.

    :param run: the run's 1-based number within its measurement.
    :param total_s: pod created → pod Ready.
    :param capacity_wait_s: pod created → the hosting NodeClaim's ``Launched``.
    :param node_launch_s: the NodeClaim's ``Launched`` → its ``Initialized``.
    :param image_pull_s: kubelet's ``Pulling`` → ``Pulled`` for the engine container.
    :param weight_download_s: the duration ``Time spent downloading weights`` reports.
    :param engine_init_s: the start of ``Loading weights took``'s duration → the ``init
        engine (profile, create kv cache, warmup model) took`` line.
    :param readiness_lag_s: the ``Starting vLLM server on`` line → pod Ready.
    :param torch_compile_s: engine init's ``torch.compile took`` duration.
    :param graph_capture_s: engine init's ``Graph capturing finished in`` durations,
        summed, since the engine captures once while profiling memory and once for real.
        vLLM logs each to the whole second.
    :param profiling_run_s: engine init's ``Initial profiling/warmup run took`` duration.
    """

    run: int
    total_s: float
    capacity_wait_s: float
    node_launch_s: float
    image_pull_s: float
    weight_download_s: float
    engine_init_s: float
    readiness_lag_s: float
    torch_compile_s: float
    graph_capture_s: float
    profiling_run_s: float

    def __post_init__(self) -> None:
        """Reject a run number below one or a negative total, phase or detail.

        A negative phase means its end marker came before its start marker, so the
        markers were misread rather than the phase taking no time.

        :raise ColdStartRecordError: when the run number or a duration is impossible.
        """
        if self.run < 1:
            raise ColdStartRecordError(
                f"cold-start run {self.run} is not a 1-based run number"
            )
        for field in fields(self):
            seconds = getattr(self, field.name)
            if field.name != "run" and seconds < 0:
                raise ColdStartRecordError(
                    f"cold-start run {self.run}'s {field.name} is {seconds}: its end "
                    f"marker is before its start marker"
                )

    @property
    def unattributed_s(self) -> float:
        """The part of the total no phase covers, to the millisecond."""
        covered = sum(getattr(self, phase) for phase in PHASES)
        return round(self.total_s - covered, SECONDS_DECIMALS)

    def to_record(self) -> dict[str, int | float]:
        """Return the record the results bucket stores.

        :return: the run number, the total, each phase, the unattributed remainder and
            engine init's detail.
        """
        return {
            "run": self.run,
            "total_s": self.total_s,
            **{phase: getattr(self, phase) for phase in PHASES},
            "unattributed_s": self.unattributed_s,
            **{detail: getattr(self, detail) for detail in ENGINE_INIT_DETAIL},
        }
