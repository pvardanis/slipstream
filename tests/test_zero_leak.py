"""Tests for the zero-leak classifier the cloud-verify teardown sweep asserts on.

Pin the money-safety contract: after teardown, an EC2 instance still billing
compute or an EBS volume still billing storage — tagged Project=slipstream —
is a leak; a terminated instance or a deleted volume is not. Malformed AWS JSON
fails loud rather than reading as "clean" and clearing a still-billing g5.
"""

from pathlib import Path

import pytest

from slipstream_bench.zero_leak import Leak, LeakError, find_leaks, read_aws_json


def _instances(*instances: dict[str, object]) -> dict[str, object]:
    """Wrap instance records in a describe-instances-shaped document."""
    return {"Reservations": [{"Instances": list(instances)}]}


def _volumes(*volumes: dict[str, object]) -> dict[str, object]:
    """Wrap volume records in a describe-volumes-shaped document."""
    return {"Volumes": list(volumes)}


def test_clean_teardown_reports_no_leaks() -> None:
    """Empty instance and volume documents are the passing case: spend is zero."""
    assert find_leaks(_instances(), _volumes()) == []


def test_running_instance_is_a_leak() -> None:
    """A still-running g5 is the exact leak the sweep exists to catch."""
    doc = _instances(
        {
            "InstanceId": "i-abc",
            "State": {"Name": "running"},
            "InstanceType": "g5.xlarge",
        }
    )

    leaks = find_leaks(doc, _volumes())

    assert leaks == [
        Leak(kind="ec2-instance", identifier="i-abc", detail="g5.xlarge running")
    ]


def test_available_volume_is_a_leak() -> None:
    """An orphaned gp3 root that outlived its instance still bills storage."""
    doc = _volumes({"VolumeId": "vol-abc", "State": "available", "Size": 100})

    leaks = find_leaks(_instances(), doc)

    assert leaks == [
        Leak(kind="ebs-volume", identifier="vol-abc", detail="100GiB available")
    ]


def test_stopping_instance_is_a_leak() -> None:
    """A stopping instance holds its billable EBS root — a leak until fully gone."""
    doc = _instances(
        {
            "InstanceId": "i-stopping",
            "State": {"Name": "stopping"},
            "InstanceType": "g5.xlarge",
        }
    )

    assert find_leaks(doc, _volumes()) == [
        Leak(kind="ec2-instance", identifier="i-stopping", detail="g5.xlarge stopping")
    ]


def test_unknown_instance_state_is_a_leak() -> None:
    """A state not on the terminal deny-list must flag, not read as clean."""
    doc = _instances(
        {
            "InstanceId": "i-weird",
            "State": {"Name": "some-future-state"},
            "InstanceType": "g5.xlarge",
        }
    )

    assert [leak.identifier for leak in find_leaks(doc, _volumes())] == ["i-weird"]


def test_creating_volume_is_a_leak() -> None:
    """A volume still being created already bills allocated storage — a leak."""
    doc = _volumes({"VolumeId": "vol-new", "State": "creating", "Size": 100})

    assert find_leaks(_instances(), doc) == [
        Leak(kind="ebs-volume", identifier="vol-new", detail="100GiB creating")
    ]


def test_error_volume_is_a_leak() -> None:
    """An error-state volume still exists and bills storage — the deny-list flags it."""
    doc = _volumes({"VolumeId": "vol-err", "State": "error", "Size": 100})

    assert find_leaks(_instances(), doc) == [
        Leak(kind="ebs-volume", identifier="vol-err", detail="100GiB error")
    ]


def test_shutting_down_instance_is_not_a_leak() -> None:
    """A shutting-down instance bills nothing on its way to terminated."""
    doc = _instances(
        {
            "InstanceId": "i-going",
            "State": {"Name": "shutting-down"},
            "InstanceType": "g5.xlarge",
        }
    )

    assert find_leaks(doc, _volumes()) == []


def test_deleted_volume_is_not_a_leak() -> None:
    """A deleted volume is gone and bills nothing."""
    doc = _volumes({"VolumeId": "vol-gone", "State": "deleted", "Size": 100})

    assert find_leaks(_instances(), doc) == []


def test_terminated_instance_is_not_a_leak() -> None:
    """A terminated instance bills nothing, so it is not a leak."""
    doc = _instances(
        {
            "InstanceId": "i-dead",
            "State": {"Name": "terminated"},
            "InstanceType": "g5.xlarge",
        }
    )

    assert find_leaks(doc, _volumes()) == []


def test_deleting_volume_is_not_a_leak() -> None:
    """A volume already being deleted is on its way out, not a leftover."""
    doc = _volumes({"VolumeId": "vol-going", "State": "deleting", "Size": 100})

    assert find_leaks(_instances(), doc) == []


def test_stopped_instance_is_a_leak() -> None:
    """A stopped instance stops billing compute but its EBS still bills — a leak."""
    doc = _instances(
        {
            "InstanceId": "i-stop",
            "State": {"Name": "stopped"},
            "InstanceType": "g5.xlarge",
        }
    )

    assert find_leaks(doc, _volumes()) == [
        Leak(kind="ec2-instance", identifier="i-stop", detail="g5.xlarge stopped")
    ]


def test_instances_and_volumes_both_reported_instances_first() -> None:
    """Every leak is reported, instances before volumes, so nothing is masked."""
    inst = _instances(
        {
            "InstanceId": "i-1",
            "State": {"Name": "running"},
            "InstanceType": "g5.xlarge",
        },
        {
            "InstanceId": "i-2",
            "State": {"Name": "pending"},
            "InstanceType": "g5.xlarge",
        },
    )
    vols = _volumes(
        {"VolumeId": "vol-1", "State": "in-use", "Size": 100},
        {"VolumeId": "vol-2", "State": "available", "Size": 8},
    )

    leaks = find_leaks(inst, vols)

    assert [leak.identifier for leak in leaks] == ["i-1", "i-2", "vol-1", "vol-2"]


def test_multiple_reservations_are_all_scanned() -> None:
    """describe-instances groups instances into reservations; scan every group."""
    doc = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-1",
                        "State": {"Name": "running"},
                        "InstanceType": "g5.xlarge",
                    }
                ]
            },
            {
                "Instances": [
                    {
                        "InstanceId": "i-2",
                        "State": {"Name": "running"},
                        "InstanceType": "g5.xlarge",
                    }
                ]
            },
        ]
    }

    assert [leak.identifier for leak in find_leaks(doc, _volumes())] == ["i-1", "i-2"]


def test_non_object_instances_document_fails_loud() -> None:
    """A non-object where a document is expected cannot be read as clean."""
    with pytest.raises(LeakError, match="not a JSON object"):
        find_leaks([], _volumes())


def test_reservations_not_a_list_fails_loud() -> None:
    """A malformed Reservations shape fails rather than silently scanning nothing."""
    with pytest.raises(LeakError, match="Reservations"):
        find_leaks({"Reservations": {}}, _volumes())


def test_instance_missing_state_fails_loud() -> None:
    """An instance without a readable state cannot be judged safe — fail loud."""
    with pytest.raises(LeakError, match="state"):
        find_leaks(_instances({"InstanceId": "i-x"}), _volumes())


def test_volume_missing_state_fails_loud() -> None:
    """A volume without a readable state cannot be judged safe — fail loud."""
    with pytest.raises(LeakError, match="state"):
        find_leaks(_instances(), _volumes({"VolumeId": "vol-x", "Size": 100}))


def test_instance_state_wrong_type_fails_loud() -> None:
    """A present-but-non-dict State cannot be judged safe — fail loud."""
    with pytest.raises(LeakError, match="state"):
        find_leaks(_instances({"InstanceId": "i-x", "State": "running"}), _volumes())


def test_billing_instance_without_id_fails_loud() -> None:
    """A still-billing instance with no InstanceId is malformed, not a nameless leak."""
    with pytest.raises(LeakError, match="no InstanceId"):
        find_leaks(_instances({"State": {"Name": "running"}}), _volumes())


def test_billing_volume_without_id_fails_loud() -> None:
    """A still-billing volume with no VolumeId is malformed, not a nameless leak."""
    with pytest.raises(LeakError, match="no VolumeId"):
        find_leaks(_instances(), _volumes({"State": "available", "Size": 100}))


def test_non_object_volumes_document_fails_loud() -> None:
    """The volumes-side shape guard fails loud, same as the instances side."""
    with pytest.raises(LeakError, match="not a JSON object"):
        find_leaks(_instances(), [])


def test_volumes_not_a_list_fails_loud() -> None:
    """A malformed Volumes shape fails rather than silently scanning nothing."""
    with pytest.raises(LeakError, match="Volumes"):
        find_leaks(_instances(), {"Volumes": {}})


def test_read_aws_json_reads_a_document(tmp_path: Path) -> None:
    """A well-formed describe-* document parses into its dict."""
    doc = tmp_path / "instances.json"
    doc.write_text('{"Reservations": []}')

    assert read_aws_json(doc) == {"Reservations": []}


def test_read_aws_json_missing_file_fails_loud(tmp_path: Path) -> None:
    """A missing sweep document cannot read as an empty (clean) result."""
    with pytest.raises(LeakError, match="cannot read AWS sweep document"):
        read_aws_json(tmp_path / "gone.json")


def test_read_aws_json_malformed_fails_loud(tmp_path: Path) -> None:
    """Bytes that are not JSON fail loud rather than surfacing as clean."""
    doc = tmp_path / "bad.json"
    doc.write_text("{not json")

    with pytest.raises(LeakError, match="cannot read AWS sweep document"):
        read_aws_json(doc)


def test_read_aws_json_non_object_fails_loud(tmp_path: Path) -> None:
    """A JSON array is not the describe-* object the sweep expects."""
    doc = tmp_path / "array.json"
    doc.write_text("[1, 2, 3]")

    with pytest.raises(LeakError, match="not a JSON object"):
        read_aws_json(doc)
