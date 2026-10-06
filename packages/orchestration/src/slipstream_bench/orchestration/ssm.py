"""Drive one shell command on the ephemeral bench host over SSM and wait for it.

ADR-0012 §Amendment: the per-cell loop lives in the orchestration layer, which runs
each cell — and the once-per-flow ``bench-proxy-up.sh`` — as one ``AWS-RunShellScript``
command against the bench host it stood up. This is the transport the flow's
per-cell ``execute_func`` calls: send the command, poll the invocation to a terminal
state, return on ``Success`` and raise :class:`SsmError` (carrying the host's stderr)
on any other outcome so the failure propagates out of the cell task uncached.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import boto3

# Invocation states SSM settles into; any other, non-terminal state — Pending,
# InProgress, Delayed, Cancelling, and so on — is still running and keeps the poll
# loop going.
_SUCCESS = "Success"
_TERMINAL = frozenset({"Success", "Cancelled", "TimedOut", "Failed"})

# The invocation may not be queryable the instant send_command returns (eventual
# consistency), so a get that raises this is treated as "not ready yet", not a failure.
_NOT_READY = "InvocationDoesNotExist"


class SsmError(Exception):
    """An SSM command that failed to run, failed on the host, or never finished."""


def run_command(
    client: Any,
    *,
    instance_id: str,
    command: str,
    timeout_s: float = 3600.0,
    poll_interval_s: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Run ``command`` on ``instance_id`` over SSM, returning only on success.

    :param client: a boto3 SSM client (or a stand-in exposing ``send_command`` and
        ``get_command_invocation``), injected so the poll loop is testable.
    :param instance_id: the bench host the command runs on.
    :param command: the shell command line (env assignments prefixed by the caller).
    :param timeout_s: the ceiling on how long to wait for a terminal state.
    :param poll_interval_s: the wait between invocation polls.
    :param sleep: the wait function, injected for tests.
    :param now: the monotonic clock, injected for tests.
    :raise SsmError: when the command cannot be sent, ends in a non-success terminal
        state (message carries the host's stderr), or does not finish within
        ``timeout_s``.
    """
    command_id = _send(client, instance_id=instance_id, command=command)
    deadline = now() + timeout_s
    while True:
        status, stderr = _poll(client, command_id=command_id, instance_id=instance_id)
        if status == _SUCCESS:
            return
        if status in _TERMINAL:
            raise SsmError(
                f"SSM command {command_id} on {instance_id} ended {status}: "
                f"{stderr.strip() or '<no stderr>'}"
            )
        if now() >= deadline:
            raise SsmError(
                f"SSM command {command_id} on {instance_id} did not finish within "
                f"{timeout_s:.0f}s (last status {status})"
            )
        sleep(poll_interval_s)


def build_ssm_client(region: str) -> Any:
    """Build a boto3 SSM client for ``region`` from the ambient credential chain.

    :param region: the AWS region the bench host runs in.
    :return: a boto3 SSM client.
    """
    return boto3.client("ssm", region_name=region)


def _send(client: Any, *, instance_id: str, command: str) -> str:
    """Send the shell command and return its command id."""
    try:
        response = client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
        )
    except Exception as error:  # surface any boto3 send failure as SsmError
        raise SsmError(
            f"could not send SSM command to {instance_id}: {error}"
        ) from error
    return response["Command"]["CommandId"]


def _poll(client: Any, *, command_id: str, instance_id: str) -> tuple[str, str]:
    """Read one invocation state, mapping a not-ready-yet get to still-pending."""
    try:
        invocation = client.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id
        )
    except Exception as error:  # not-ready is benign; any other get failure is fatal
        if _is_not_ready(error):
            return "Pending", ""
        raise SsmError(
            f"could not read SSM invocation {command_id} on {instance_id}: {error}"
        ) from error
    status = invocation.get("Status")
    if not status:
        raise SsmError(
            f"SSM invocation {command_id} on {instance_id} returned no status "
            f"(response {invocation!r}): the invocation shape is unexpected"
        )
    return status, invocation.get("StandardErrorContent", "")


def _is_not_ready(error: Exception) -> bool:
    """Report whether a get failure is SSM's not-created-yet eventual-consistency case.

    Matched precisely — by boto3's modeled exception class name, or the structured
    error code botocore carries on the response — never by a substring of the message,
    so an unrelated failure that merely mentions the code is not misread as
    still-pending and polled all the way to the timeout.

    :param error: the exception ``get_command_invocation`` raised.
    :return: True only for the ``InvocationDoesNotExist`` not-ready case.
    """
    if type(error).__name__ == _NOT_READY:
        return True
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code") == _NOT_READY
    return False
