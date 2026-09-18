"""Classify AWS teardown leftovers the cloud-verify sweep asserts are gone.

After `cloud-verify` tears the GPU path down, a `Project=slipstream` EC2 instance
or EBS volume that survives is a still-billing leak — the money-safety failure
ADR-0002 ranks first, and one that lives outside Terraform state (Karpenter
launches the g5 through an in-cluster controller). This reads the two
Project-tag-filtered ``aws ec2 describe-*`` documents the sweep collects and
names anything in a still-billing state — so the billing-state set lives here
alone, not also in the sweep's query. Malformed input raises rather than reading
as "clean", so a broken query can never green-light a running g5.
"""

import json
from dataclasses import dataclass
from pathlib import Path

# Instance states that still bill: compute while pending/running/stopping, and
# the backing EBS while stopped. terminated and shutting-down bill nothing and
# are on their way out, so they are not leaks.
_ACTIVE_INSTANCE_STATES = frozenset({"pending", "running", "stopping", "stopped"})

# Volume states that still bill storage. deleting/deleted are on their way out.
_ACTIVE_VOLUME_STATES = frozenset({"creating", "available", "in-use"})


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
            if name in _ACTIVE_INSTANCE_STATES:
                leaks.append(
                    Leak(
                        kind="ec2-instance",
                        identifier=str(instance.get("InstanceId")),
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
        if state in _ACTIVE_VOLUME_STATES:
            leaks.append(
                Leak(
                    kind="ebs-volume",
                    identifier=str(volume.get("VolumeId")),
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
