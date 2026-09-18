"""Classify AWS teardown leftovers the cloud-verify sweep asserts are gone.

After `cloud-verify` tears the GPU path down, a `Project=slipstream` EC2 instance
or EBS volume that survives is a still-billing leak — the money-safety failure
ADR-0002 ranks first, and one that lives outside Terraform state (Karpenter
launches the g5 through an in-cluster controller). This reads the two
Project-tag-filtered ``aws ec2 describe-*`` documents the sweep collects and
names anything not in a known-terminal state — a deny-list, so an ``error`` volume
or a state AWS adds later reads as a leak, never silently as clean. The state sets
live here alone, not also in the sweep's query. Malformed input raises rather than
reading as "clean", so a broken query can never green-light a running g5.
"""

import json
from dataclasses import dataclass
from pathlib import Path

# Instance states that bill nothing and are on their way out. The classifier flags
# anything NOT listed here: running bills compute, pending is about to, and
# stopping/stopped still hold the billable EBS root — plus any state AWS adds later.
# A deny-list fails toward flagging: an unrecognized state must read as a leak,
# never silently as clean. Assumes standard on-demand billing (no hibernation).
_TERMINAL_INSTANCE_STATES = frozenset({"shutting-down", "terminated"})

# Volume states that bill nothing (on their way out). Everything else is a leak —
# creating, available, in-use, and error (a failed volume still bills for its
# allocated storage). Deny-list for the same fail-toward-flagging reason as instances.
_GONE_VOLUME_STATES = frozenset({"deleting", "deleted"})


class LeakError(Exception):
    """AWS JSON that cannot be read as the describe-instances/volumes the sweep expects."""


@dataclass(frozen=True)
class Leak:
    """One still-billing resource that outlived teardown."""

    kind: str
    identifier: str
    detail: str


def _require_dict(document: object, name: str) -> dict:
    """Return the document as a dict or fail loud — a scalar cannot read as clean."""
    if not isinstance(document, dict):
        raise LeakError(f"{name} is not a JSON object")
    return document


def _require_list(value: object, name: str) -> list:
    """Return the value as a list or fail loud — a wrong shape must not scan as empty."""
    if not isinstance(value, list):
        raise LeakError(f"{name} is not a list")
    return value


def _instance_leaks(instances_doc: dict) -> list[Leak]:
    """Name every still-billing instance across all reservations."""
    reservations = _require_list(instances_doc.get("Reservations", []), "Reservations")
    leaks: list[Leak] = []
    for reservation in reservations:
        for instance in _require_list(
            _require_dict(reservation, "reservation").get("Instances", []), "Instances"
        ):
            instance = _require_dict(instance, "instance")
            state = instance.get("State", {})
            name = state.get("Name") if isinstance(state, dict) else None
            if not isinstance(name, str):
                raise LeakError(
                    f"instance {instance.get('InstanceId')} has no readable state"
                )
            if name not in _TERMINAL_INSTANCE_STATES:
                instance_id = instance.get("InstanceId")
                if not isinstance(instance_id, str):
                    raise LeakError(
                        f"still-billing instance in state {name!r} has no InstanceId"
                    )
                leaks.append(
                    Leak(
                        kind="ec2-instance",
                        identifier=instance_id,
                        detail=f"{instance.get('InstanceType', 'unknown')} {name}",
                    )
                )
    return leaks


def _volume_leaks(volumes_doc: dict) -> list[Leak]:
    """Name every still-billing volume."""
    leaks: list[Leak] = []
    for volume in _require_list(volumes_doc.get("Volumes", []), "Volumes"):
        volume = _require_dict(volume, "volume")
        state = volume.get("State")
        if not isinstance(state, str):
            raise LeakError(f"volume {volume.get('VolumeId')} has no readable state")
        if state not in _GONE_VOLUME_STATES:
            volume_id = volume.get("VolumeId")
            if not isinstance(volume_id, str):
                raise LeakError(
                    f"still-billing volume in state {state!r} has no VolumeId"
                )
            leaks.append(
                Leak(
                    kind="ebs-volume",
                    identifier=volume_id,
                    detail=f"{volume.get('Size', 'unknown')}GiB {state}",
                )
            )
    return leaks


def read_aws_json(path: Path) -> dict:
    """Read one ``aws ec2 describe-*`` document the sweep captured.

    :param path: the JSON file ``aws`` wrote.
    :return: the parsed top-level JSON object.
    :raise LeakError: when the file is unreadable, not JSON, or not a JSON object
        — a broken sweep query must fail loud, not read as an empty (clean) result.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LeakError(f"cannot read AWS sweep document {path}: {error}") from error
    if not isinstance(data, dict):
        raise LeakError(f"AWS sweep document {path} is not a JSON object")
    return data


def find_leaks(instances_doc: object, volumes_doc: object) -> list[Leak]:
    """Classify tagged teardown leftovers, instances before volumes.

    :param instances_doc: parsed ``aws ec2 describe-instances`` JSON, already
        filtered to the ``Project=slipstream`` tag by the sweep.
    :param volumes_doc: parsed ``aws ec2 describe-volumes`` JSON, same tag filter.
    :return: every still-billing instance then every still-billing volume; empty
        when teardown returned spend to zero.
    :raise LeakError: when either document is not the expected shape.
    """
    instances = _require_dict(instances_doc, "instances document")
    volumes = _require_dict(volumes_doc, "volumes document")
    return _instance_leaks(instances) + _volume_leaks(volumes)
