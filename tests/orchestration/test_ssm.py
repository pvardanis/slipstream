"""The SSM transport: send one shell command to the bench host, poll it to done.

ADR-0012 §Amendment: the orchestration layer drives each cell as one command over
SSM against the ephemeral bench host. These tests exercise the poll loop against a
scripted fake SSM client — send returns a command id, a sequence of invocation
statuses walks Pending -> InProgress -> a terminal state — so the classify-and-raise
logic is covered without a live AWS call. A Success returns; any other terminal state
raises SsmError carrying the host's stderr; a run that never finishes trips the
timeout.
"""

from collections.abc import Iterator

import pytest

from slipstream_bench.orchestration.ssm import SsmError, run_command


class _FakeSsm:
    """A scripted SSM client: one send, then a fixed sequence of invocation states.

    ``statuses`` is walked one per ``get_command_invocation`` call; the last entry
    repeats if polled past the script. Each entry is ``(status, stderr)``.
    """

    def __init__(self, statuses: list[tuple[str, str]]) -> None:
        self._statuses = statuses
        self._index = 0
        self.sent: list[dict[str, object]] = []

    def send_command(self, **kwargs: object) -> dict[str, object]:
        self.sent.append(kwargs)
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, **_kwargs: object) -> dict[str, object]:
        status, stderr = self._statuses[min(self._index, len(self._statuses) - 1)]
        self._index += 1
        return {"Status": status, "StandardErrorContent": stderr}


def _no_wait() -> Iterator[float]:
    tick = 0.0
    while True:
        yield tick
        tick += 1.0


def test_run_command_returns_on_success() -> None:
    client = _FakeSsm([("Pending", ""), ("InProgress", ""), ("Success", "")])

    run_command(
        client,
        instance_id="i-123",
        command="/usr/local/bin/bench-sweep.sh",
        poll_interval_s=0.0,
        sleep=lambda _s: None,
    )

    assert client.sent[0]["InstanceIds"] == ["i-123"]
    assert client.sent[0]["Parameters"] == {
        "commands": ["/usr/local/bin/bench-sweep.sh"]
    }


def test_run_command_raises_on_a_failed_invocation() -> None:
    client = _FakeSsm([("InProgress", ""), ("Failed", "bench-cell: docker failed")])

    with pytest.raises(SsmError, match="docker failed"):
        run_command(
            client,
            instance_id="i-123",
            command="cmd",
            poll_interval_s=0.0,
            sleep=lambda _s: None,
        )


def test_run_command_times_out_when_never_terminal() -> None:
    client = _FakeSsm([("InProgress", "")])
    clock = _no_wait()

    with pytest.raises(SsmError, match="did not finish"):
        run_command(
            client,
            instance_id="i-123",
            command="cmd",
            timeout_s=3.0,
            poll_interval_s=1.0,
            sleep=lambda _s: None,
            now=lambda: next(clock),
        )
