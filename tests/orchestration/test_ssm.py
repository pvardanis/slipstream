"""The SSM transport: send one shell command to the bench host, poll it to done.

ADR-0012 §Amendment: the orchestration layer drives each cell as one command over
SSM against the ephemeral bench host. These tests exercise the poll loop against a
scripted fake SSM client — send returns a command id, a sequence of invocation
statuses walks Pending -> InProgress -> a terminal state — so the classify-and-raise
logic is covered without a live AWS call. A Success returns; any other terminal state
raises SsmError carrying the host's stderr; a run that never finishes trips the
timeout. Separate scripted fakes cover the eventual-consistency not-ready poll, a send
that fails, a get that fails for another reason, and an invocation with no status.
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


class InvocationDoesNotExist(Exception):
    """Stands in for boto3's modeled not-ready exception (matched by class name)."""


class _NotReadyThenSuccess:
    """The invocation is not queryable on the first poll, then reports Success."""

    def __init__(self) -> None:
        self.polls = 0

    def send_command(self, **_kwargs: object) -> dict[str, object]:
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, **_kwargs: object) -> dict[str, object]:
        self.polls += 1
        if self.polls == 1:
            raise InvocationDoesNotExist("invocation not created yet")
        return {"Status": "Success", "StandardErrorContent": ""}


class _SendFails:
    def send_command(self, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("ThrottlingException: rate exceeded")

    def get_command_invocation(self, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("unreachable: send failed")


class _ReadFails:
    def send_command(self, **_kwargs: object) -> dict[str, object]:
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("AccessDeniedException")


class _NoStatus:
    def send_command(self, **_kwargs: object) -> dict[str, object]:
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, **_kwargs: object) -> dict[str, object]:
        return {"StandardErrorContent": ""}


def _run(client: object) -> None:
    run_command(
        client,
        instance_id="i-123",
        command="cmd",
        poll_interval_s=0.0,
        sleep=lambda _s: None,
    )


def test_run_command_polls_through_a_not_ready_invocation() -> None:
    client = _NotReadyThenSuccess()

    _run(client)

    assert client.polls == 2  # first poll not-ready, second Success


def test_run_command_rejects_a_not_ready_lookalike_message() -> None:
    # A ThrottlingException whose text incidentally mentions the not-ready code must
    # not be misread as still-pending and polled to the timeout.
    class _LookalikeThenNever:
        def send_command(self, **_kwargs: object) -> dict[str, object]:
            return {"Command": {"CommandId": "cmd-1"}}

        def get_command_invocation(self, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError("Throttled while checking InvocationDoesNotExist")

    with pytest.raises(SsmError, match="could not read"):
        _run(_LookalikeThenNever())


def test_run_command_raises_when_the_command_cannot_be_sent() -> None:
    with pytest.raises(SsmError, match="could not send"):
        _run(_SendFails())


def test_run_command_raises_when_the_invocation_cannot_be_read() -> None:
    with pytest.raises(SsmError, match="could not read"):
        _run(_ReadFails())


def test_run_command_raises_on_an_invocation_with_no_status() -> None:
    with pytest.raises(SsmError, match="returned no status"):
        _run(_NoStatus())
