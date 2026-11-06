"""The bench-endpoint session: apply the stack, wait for the bench host to boot, always destroy.

These tests drive the session against a scripted fake Terraform, which records each argv and
the environment it was handed and answers ``output`` with a bench host instance id, and a fake
S3 serving the host's boot sentinel. So the lifecycle (destroy after any outcome once the stack
may hold resources), the sentinel handling, and where the variables travel are covered with
no AWS and no Terraform binary.
"""

import io
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from slipstream_bench.orchestration.bench_endpoint import (
    BenchEndpointTopology,
    BenchHost,
    open_bench_endpoint,
)

_INSTANCE_ID = "i-0abc123"

_TOPOLOGY = BenchEndpointTopology(
    state_bucket="slipstream-tfstate-x1",
    vpc_id="vpc-1",
    public_subnets=("subnet-a", "subnet-b"),
    node_security_group_id="sg-node",
    node_autoscaling_groups=("asg-1",),
    instance_profile="slipstream-bench-host",
)


class _FakeTerraform:
    """A scripted Terraform: records every call and answers ``output`` with an instance id.

    ``fail_on`` names a subcommand (``init``, ``apply``, ``destroy``) that raises instead of
    succeeding, standing in for a non-zero exit.
    """

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self._fail_on = fail_on

    def __call__(self, args: Sequence[str], env: Mapping[str, str]) -> str:
        self.calls.append((list(args), dict(env)))
        subcommand = _subcommand(args)
        if subcommand == self._fail_on:
            raise RuntimeError(f"terraform {subcommand} failed")
        if subcommand == "output":
            return f"{_INSTANCE_ID}\n"
        return ""

    def subcommands(self) -> list[str]:
        return [_subcommand(args) for args, _env in self.calls]


def _subcommand(args: Sequence[str]) -> str:
    """Return the first argument that is not a global ``-flag``, the Terraform subcommand."""
    return next(arg for arg in args if not arg.startswith("-"))


class _FakeS3:
    """Serve a scripted sequence of boot sentinels, one per ``get_object``.

    ``None`` stands for the sentinel not written yet (S3's NoSuchKey); the last entry repeats
    if polled past the script. The keys read are recorded.
    """

    def __init__(self, sentinels: list[str | None]) -> None:
        self._sentinels = sentinels
        self._index = 0
        self.reads: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.reads.append((Bucket, Key))
        sentinel = self._sentinels[min(self._index, len(self._sentinels) - 1)]
        self._index += 1
        if sentinel is None:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "absent"}}, "GetObject"
            )
        return {"Body": io.BytesIO(sentinel.encode())}


@pytest.fixture
def stack_dir(tmp_path: Path) -> Path:
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / "main.tf").write_text("# stack\n", encoding="utf-8")
    return stack


def _open(
    terraform: _FakeTerraform,
    s3: _FakeS3,
    stack_dir: Path,
    *,
    boot_timeout_s: float = 420.0,
):
    return open_bench_endpoint(
        terraform=terraform,
        s3=s3,
        stack_dir=stack_dir,
        topology=_TOPOLOGY,
        api_key="sk-secret-key",
        results_bucket="bench-results-x1",
        boot_timeout_s=boot_timeout_s,
        sleep=lambda _s: None,
    )


def test_session_yields_the_bench_host_and_destroys_after_the_body(
    stack_dir: Path,
) -> None:
    terraform = _FakeTerraform()

    with _open(terraform, _FakeS3(["ok"]), stack_dir) as host:
        assert host == BenchHost(instance_id=_INSTANCE_ID)
        assert "destroy" not in terraform.subcommands()

    assert terraform.subcommands() == ["init", "apply", "output", "destroy"]


def test_session_destroys_after_a_raising_body(stack_dir: Path) -> None:
    terraform = _FakeTerraform()

    with (
        pytest.raises(ValueError, match="cell failed"),
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        raise ValueError("cell failed")

    assert terraform.subcommands()[-1] == "destroy"


def test_session_destroys_after_a_failed_apply(stack_dir: Path) -> None:
    # A failed apply can leave part of the stack created, so it is torn down too.
    terraform = _FakeTerraform(fail_on="apply")

    with (
        pytest.raises(RuntimeError, match="apply failed"),
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        pytest.fail("the body must not run after a failed apply")

    assert terraform.subcommands() == ["init", "apply", "destroy"]


def test_session_does_not_destroy_after_a_failed_init(stack_dir: Path) -> None:
    # init changes no infrastructure, and a destroy without an initialised backend would
    # fail too and bury the init error.
    terraform = _FakeTerraform(fail_on="init")

    with (
        pytest.raises(RuntimeError, match="init failed"),
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        pytest.fail("the body must not run after a failed init")

    assert terraform.subcommands() == ["init"]


def test_failed_destroy_after_a_raising_body_keeps_the_body_error_as_context(
    stack_dir: Path,
) -> None:
    # A failed destroy leaves resources billing, so it is the error raised; the body's
    # failure stays on it as context rather than being lost.
    terraform = _FakeTerraform(fail_on="destroy")

    with (
        pytest.raises(RuntimeError, match="destroy failed") as excinfo,
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        raise ValueError("cell failed")

    assert isinstance(excinfo.value.__context__, ValueError)
