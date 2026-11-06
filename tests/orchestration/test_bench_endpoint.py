"""The bench-endpoint session: apply the stack, wait for the bench host to boot, always destroy.

These tests drive the session against a scripted fake Terraform, which records each argv and
the environment it was handed and answers ``output`` with a bench host instance id, and a fake
S3 serving the host's boot sentinel. So the lifecycle (destroy after any outcome once the stack
may hold resources), the sentinel handling, and where the variables travel are covered with
no AWS and no Terraform binary.
"""

import io
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from slipstream_bench.orchestration.bench_endpoint import (
    BenchEndpointTopology,
    BenchHost,
    BenchHostBootError,
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
    now: Callable[[], float] = lambda: 0.0,
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
        now=now,
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


_EXPECTED_VARIABLES = {
    "TF_VAR_state_bucket": "slipstream-tfstate-x1",
    "TF_VAR_vpc_id": "vpc-1",
    "TF_VAR_public_subnets": '["subnet-a", "subnet-b"]',
    "TF_VAR_node_security_group_id": "sg-node",
    "TF_VAR_node_autoscaling_groups": '["asg-1"]',
    "TF_VAR_bench_host_instance_profile": "slipstream-bench-host",
    "TF_VAR_vllm_api_key": "sk-secret-key",
}


def test_topology_and_api_key_reach_apply_and_destroy_as_variables(
    stack_dir: Path,
) -> None:
    terraform = _FakeTerraform()

    with _open(terraform, _FakeS3(["ok"]), stack_dir):
        pass

    for args, env in terraform.calls:
        if _subcommand(args) in {"apply", "destroy"}:
            assert env == _EXPECTED_VARIABLES, args


def test_api_key_never_appears_in_terraform_argv(stack_dir: Path) -> None:
    # argv is visible to every process on the node through ps; the environment is not.
    terraform = _FakeTerraform()

    with _open(terraform, _FakeS3(["ok"]), stack_dir):
        pass

    assert terraform.calls
    for args, _env in terraform.calls:
        assert not any("sk-secret-key" in arg for arg in args), args


def test_init_points_the_backend_at_the_state_bucket_without_prompting(
    stack_dir: Path,
) -> None:
    terraform = _FakeTerraform()

    with _open(terraform, _FakeS3(["ok"]), stack_dir):
        pass

    init_args = terraform.calls[0][0]
    assert _subcommand(init_args) == "init"
    assert "-backend-config=bucket=slipstream-tfstate-x1" in init_args
    assert "-input=false" in init_args


def test_apply_and_destroy_run_unattended(stack_dir: Path) -> None:
    terraform = _FakeTerraform()

    with _open(terraform, _FakeS3(["ok"]), stack_dir):
        pass

    for args, _env in terraform.calls:
        if _subcommand(args) in {"apply", "destroy"}:
            assert {"-auto-approve", "-input=false"} <= set(args), args


def test_session_reads_the_bench_host_instance_id_output(stack_dir: Path) -> None:
    terraform = _FakeTerraform()

    with _open(terraform, _FakeS3(["ok"]), stack_dir):
        pass

    (output_args,) = [
        args for args, _env in terraform.calls if _subcommand(args) == "output"
    ]
    assert output_args[-2:] == ["-raw", "bench_host_instance_id"]


def test_terraform_runs_in_a_writable_copy_of_the_stack(stack_dir: Path) -> None:
    # The image bakes the stack on a read-only root, so terraform cannot write .terraform
    # beside it; every command runs in a copy holding the stack's files.
    seen: list[Path] = []

    def terraform(args: Sequence[str], env: Mapping[str, str]) -> str:
        workdir = Path(args[0].removeprefix("-chdir="))
        assert workdir != stack_dir
        assert (workdir / "main.tf").read_text(encoding="utf-8") == "# stack\n"
        seen.append(workdir)
        return f"{_INSTANCE_ID}\n" if _subcommand(args) == "output" else ""

    with open_bench_endpoint(
        terraform=terraform,
        s3=_FakeS3(["ok"]),
        stack_dir=stack_dir,
        topology=_TOPOLOGY,
        api_key="sk-secret-key",
        results_bucket="bench-results-x1",
        sleep=lambda _s: None,
    ):
        pass

    assert len(set(seen)) == 1
    assert not seen[0].exists()


def _ticking_clock(step_s: float) -> Callable[[], float]:
    """A monotonic clock that advances ``step_s`` on every read."""
    ticks: Iterator[float] = (index * step_s for index in range(10_000))
    return lambda: next(ticks)


def test_session_waits_for_the_boot_sentinel_before_yielding(stack_dir: Path) -> None:
    # The host writes its sentinel when cloud-init finishes; until then the key is absent.
    s3 = _FakeS3([None, None, "ok"])

    with _open(_FakeTerraform(), s3, stack_dir) as host:
        assert len(s3.reads) == 3

    assert host.instance_id == _INSTANCE_ID
    assert s3.reads[0] == ("bench-results-x1", f"boot-status/{_INSTANCE_ID}")


def test_failed_sentinel_raises_with_the_step_and_destroys(stack_dir: Path) -> None:
    terraform = _FakeTerraform()

    with (
        pytest.raises(BenchHostBootError, match="failed: pull bench image"),
        _open(terraform, _FakeS3([None, "failed: pull bench image"]), stack_dir),
    ):
        pytest.fail("the body must not run on a host that failed to boot")

    assert terraform.subcommands()[-1] == "destroy"


def test_sentinel_timeout_raises_and_destroys(stack_dir: Path) -> None:
    terraform = _FakeTerraform()

    with (
        pytest.raises(BenchHostBootError, match="did not report ready within 420s"),
        _open(terraform, _FakeS3([None]), stack_dir, now=_ticking_clock(step_s=60.0)),
    ):
        pytest.fail("the body must not run on a host that never booted")

    assert terraform.subcommands()[-1] == "destroy"


def test_unreadable_sentinel_raises_at_once_and_destroys(stack_dir: Path) -> None:
    # Only an absent sentinel means not booted yet; a denied read would never clear, so it
    # raises rather than polling to the timeout.
    class _DeniedS3:
        def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject"
            )

    terraform = _FakeTerraform()

    with (
        pytest.raises(ClientError, match="AccessDenied"),
        open_bench_endpoint(
            terraform=terraform,
            s3=_DeniedS3(),
            stack_dir=stack_dir,
            topology=_TOPOLOGY,
            api_key="sk-secret-key",
            results_bucket="bench-results-x1",
            sleep=lambda _s: pytest.fail("a denied read must not be retried"),
        ),
    ):
        pytest.fail("the body must not run")

    assert terraform.subcommands()[-1] == "destroy"


def test_empty_instance_id_output_raises_and_destroys(stack_dir: Path) -> None:
    calls: list[str] = []

    def terraform(args: Sequence[str], env: Mapping[str, str]) -> str:
        calls.append(_subcommand(args))
        return ""

    with (
        pytest.raises(BenchHostBootError, match="bench_host_instance_id"),
        open_bench_endpoint(
            terraform=terraform,
            s3=_FakeS3(["ok"]),
            stack_dir=stack_dir,
            topology=_TOPOLOGY,
            api_key="sk-secret-key",
            results_bucket="bench-results-x1",
            sleep=lambda _s: None,
        ),
    ):
        pytest.fail("the body must not run without a bench host")

    assert calls[-1] == "destroy"
