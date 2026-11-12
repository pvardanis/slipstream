"""The bench-endpoint session: apply the stack, wait for the bench host to boot, always destroy.

These tests drive the session against a scripted fake Terraform, which records each argv and
the environment it was handed and answers ``output`` with a bench host instance id, and a fake
S3 serving the host's boot sentinel. So the lifecycle (destroy after any outcome once the stack
may hold resources), the sentinel handling, where the variables travel, and the milestones
it logs are covered with no AWS and no Terraform binary.
"""

import io
import logging
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from prefect.exceptions import TerminationSignal

from slipstream_bench.orchestration.bench_endpoint import (
    BenchEndpointEnvironmentError,
    BenchEndpointInUseError,
    BenchEndpointLockedError,
    BenchEndpointTopology,
    BenchHost,
    BenchHostBootError,
    TerraformError,
    build_terraform,
    destroy_bench_endpoint,
    open_bench_endpoint,
    read_stack_dir,
    read_topology,
)

_INSTANCE_ID = "i-0abc123"
_RUN_ID = "run-42"

# The wall clock the session stamps an errored-state copy with, and the stamp it yields.
_CLOCK = datetime(2026, 11, 6, 15, 33, 35, 353955, tzinfo=UTC)
_STAMP = "20261106T153335353955Z"


def _errored_key(subcommand: str, stamp: str = _STAMP) -> str:
    return f"bench-endpoint/errored/{_RUN_ID}/{subcommand}-{stamp}.tfstate"


_TOPOLOGY = BenchEndpointTopology(
    state_bucket="slipstream-tfstate-x1",
    vpc_id="vpc-1",
    public_subnets=("subnet-a", "subnet-b"),
    node_security_group_id="sg-node",
    node_autoscaling_groups=("asg-1",),
    instance_profile="slipstream-bench-host",
)


# `terraform show -json` on a backend holding no state, and on one holding a resource.
_EMPTY_STATE = '{"format_version":"1.0"}\n'
_HELD_STATE = (
    '{"format_version":"1.0","values":{"root_module":{"resources":'
    '[{"address":"aws_lb.bench","mode":"managed"}]}}}\n'
)


_LOCK_ID = "2dcc6d8e-8b22-9868-6f34-a1538a71338d"

# What terraform writes to stderr when another process holds the state lock, in the shape
# Terraform prints it; the production transport carries stderr in its TerraformError message.
_LOCK_STDERR = f"""Error: Error acquiring the state lock

Error message: operation error S3: PutObject, StatusCode: 412, PreconditionFailed
Lock Info:
  ID:        {_LOCK_ID}
  Path:      slipstream-tfstate-x1/bench-endpoint/terraform.tfstate
  Operation: OperationTypeApply
  Who:       root@prefect-worker-0
  Version:   1.17.1
  Created:   2026-11-06 15:33:35.353955 +0000 UTC
  Info:

Terraform acquires a state lock to protect the state from being written
by multiple users at the same time."""


class _FakeTerraform:
    """A scripted Terraform: records every call and answers ``output`` with an instance id.

    ``fail_on`` names a subcommand (``init``, ``apply``, ``destroy``) that raises instead of
    succeeding, standing in for a non-zero exit. ``locked_on`` names one that fails because
    another process holds the state lock, and ``errored_on`` one that fails after writing
    ``errored.tfstate`` to its working directory, as terraform does when it cannot push the
    state it changed. ``show`` answers with the state the backend already holds: none by
    default, or ``existing_state``.
    """

    def __init__(
        self,
        *,
        fail_on: str | None = None,
        locked_on: str | None = None,
        errored_on: str | None = None,
        existing_state: str = _EMPTY_STATE,
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self._fail_on = fail_on
        self._locked_on = locked_on
        self._errored_on = errored_on
        self._existing_state = existing_state

    def __call__(self, args: Sequence[str], env: Mapping[str, str]) -> str:
        self.calls.append((list(args), dict(env)))
        subcommand = _subcommand(args)
        if subcommand == self._locked_on:
            raise TerraformError(f"terraform {' '.join(args)} exited 1: {_LOCK_STDERR}")
        if subcommand == self._errored_on:
            workdir = Path(args[0].removeprefix("-chdir="))
            (workdir / "errored.tfstate").write_text(
                _errored_state(subcommand), encoding="utf-8"
            )
            raise RuntimeError(f"terraform {subcommand} failed to push its state")
        if subcommand == self._fail_on:
            raise RuntimeError(f"terraform {subcommand} failed")
        if subcommand == "output":
            return f"{_INSTANCE_ID}\n"
        if subcommand == "show":
            return self._existing_state
        return ""

    def subcommands(self) -> list[str]:
        return [_subcommand(args) for args, _env in self.calls]


def _errored_state(subcommand: str) -> str:
    """The state a failed ``subcommand`` dumps to errored.tfstate, distinct per command."""
    return f'{{"version":4,"serial":7,"lineage":"{subcommand}"}}\n'


def _subcommand(args: Sequence[str]) -> str:
    """Return the first argument that is not a global ``-flag``, the Terraform subcommand."""
    return next(arg for arg in args if not arg.startswith("-"))


class _FakeS3:
    """Serve a scripted sequence of boot sentinels, one per ``get_object``.

    ``None`` stands for the sentinel not written yet (S3's NoSuchKey); the last entry repeats
    if polled past the script. Each body ends in the newline the host's ``<<<`` here-string
    writes (``user-data.sh.tftpl``). The keys read are recorded, and every object put is
    kept by bucket and key; ``put_error`` makes each put raise instead.
    """

    def __init__(
        self, sentinels: list[str | None], *, put_error: Exception | None = None
    ) -> None:
        self._sentinels = sentinels
        self._index = 0
        self._put_error = put_error
        self.reads: list[tuple[str, str]] = []
        self.puts: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, object]:
        if self._put_error is not None:
            raise self._put_error
        self.puts[(Bucket, Key)] = Body
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.reads.append((Bucket, Key))
        sentinel = self._sentinels[min(self._index, len(self._sentinels) - 1)]
        self._index += 1
        if sentinel is None:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "absent"}}, "GetObject"
            )
        return {"Body": io.BytesIO(f"{sentinel}\n".encode())}


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
    sleep: Callable[[float], None] = lambda _s: None,
    wall_clock: Callable[[], datetime] = lambda: _CLOCK,
):
    return open_bench_endpoint(
        terraform=terraform,
        s3_client=s3,
        stack_dir=stack_dir,
        topology=_TOPOLOGY,
        api_key="sk-secret-key",
        results_bucket="bench-results-x1",
        run_id=_RUN_ID,
        boot_timeout_s=boot_timeout_s,
        sleep=sleep,
        now=now,
        wall_clock=wall_clock,
    )


def test_session_yields_the_bench_host_and_destroys_after_the_body(
    stack_dir: Path,
) -> None:
    terraform = _FakeTerraform()

    with _open(terraform, _FakeS3(["ok"]), stack_dir) as host:
        assert host == BenchHost(instance_id=_INSTANCE_ID)
        assert "destroy" not in terraform.subcommands()

    assert terraform.subcommands() == ["init", "show", "apply", "output", "destroy"]


def test_session_destroys_after_a_raising_body(stack_dir: Path) -> None:
    terraform = _FakeTerraform()

    with (
        pytest.raises(ValueError, match="cell failed"),
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        raise ValueError("cell failed")

    assert terraform.subcommands()[-1] == "destroy"


@pytest.mark.parametrize("signal", [TerminationSignal, KeyboardInterrupt])
def test_session_leaves_the_destroy_to_the_crash_hook_when_the_run_is_killed(
    stack_dir: Path, signal: type[BaseException]
) -> None:
    # A run being killed has seconds left: a destroy started now is cut off holding the
    # state lock, which then blocks the flow's crash or cancellation hook from destroying
    # the endpoint after the session exits. So the session leaves the destroy to that hook.
    terraform = _FakeTerraform()

    with pytest.raises(signal), _open(terraform, _FakeS3(["ok"]), stack_dir):
        raise signal(*((15,) if signal is TerminationSignal else ()))

    assert "destroy" not in terraform.subcommands()


def test_session_logs_that_it_left_the_endpoint_for_the_hook(
    stack_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    with (
        pytest.raises(TerminationSignal),
        _open(_FakeTerraform(), _FakeS3(["ok"]), stack_dir),
    ):
        raise TerminationSignal(15)

    [record] = [
        r
        for r in caplog.records
        if "crash, cancellation or timeout hook" in r.getMessage()
    ]
    # Logged as an error naming the state and the recipe that destroys from it, since a
    # signal no hook answers leaves the endpoint billing with this line its only trace.
    assert record.levelno == logging.ERROR
    assert (
        "s3://slipstream-tfstate-x1/bench-endpoint/terraform.tfstate"
        in record.getMessage()
    )
    assert "just bench-endpoint-down" in record.getMessage()
    # Names the line the hook logs on the run once it has destroyed the endpoint, so its
    # absence is what calls for the recipe. The line is named in its general form, so a
    # search of the run's logs for the hook's line finds the hook's and not this one.
    assert "destroyed run <run-id>'s bench endpoint" in record.getMessage()
    assert f"destroyed run {_RUN_ID}'s bench endpoint" not in record.getMessage()


def test_session_destroys_after_a_failed_apply(stack_dir: Path) -> None:
    # A failed apply can leave part of the stack created, so it is torn down too.
    terraform = _FakeTerraform(fail_on="apply")

    with (
        pytest.raises(RuntimeError, match="apply failed"),
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        pytest.fail("the body must not run after a failed apply")

    assert terraform.subcommands() == ["init", "show", "apply", "destroy"]


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


def test_failed_destroy_after_a_failed_apply_keeps_the_apply_error_as_context(
    stack_dir: Path,
) -> None:
    class _ApplyAndDestroyFail(_FakeTerraform):
        def __call__(self, args: Sequence[str], env: Mapping[str, str]) -> str:
            if _subcommand(args) == "apply":
                self.calls.append((list(args), dict(env)))
                raise ValueError("terraform apply failed")
            return super().__call__(args, env)

    terraform = _ApplyAndDestroyFail(fail_on="destroy")

    with (
        pytest.raises(RuntimeError, match="destroy failed") as excinfo,
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        pytest.fail("the body must not run after a failed apply")

    assert isinstance(excinfo.value.__context__, ValueError)


def test_failed_destroy_names_the_state_left_behind(stack_dir: Path) -> None:
    # A failed destroy leaves the endpoint billing; its error names where the state lives
    # so the operator can destroy from it.
    terraform = _FakeTerraform(fail_on="destroy")

    with (
        pytest.raises(RuntimeError) as excinfo,
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        pass

    notes = "\n".join(getattr(excinfo.value, "__notes__", []))
    assert "may still be billing" in notes
    assert "s3://slipstream-tfstate-x1/bench-endpoint/terraform.tfstate" in notes


def test_session_refuses_a_backend_that_already_holds_an_endpoint(
    stack_dir: Path,
) -> None:
    # The state key is shared with `just bench-endpoint-up` and every other run, so an
    # endpoint already in it belongs to someone else: applying would adopt it and the exit
    # would destroy it under them. The session raises without applying or destroying.
    terraform = _FakeTerraform(existing_state=_HELD_STATE)

    with (
        pytest.raises(
            BenchEndpointInUseError,
            match="s3://slipstream-tfstate-x1/bench-endpoint/terraform.tfstate",
        ),
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        pytest.fail("the body must not run against another run's endpoint")

    assert terraform.subcommands() == ["init", "show"]


def test_held_lock_raises_with_the_lock_id_and_the_force_unlock_command(
    stack_dir: Path,
) -> None:
    # A lock left by a killed run blocks every later apply; the error hands the operator
    # the id and the command so recovery needs no digging through terraform output.
    terraform = _FakeTerraform(locked_on="apply")

    with (
        pytest.raises(BenchEndpointLockedError) as excinfo,
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        pytest.fail("the body must not run without the state lock")

    message = str(excinfo.value)
    assert _LOCK_ID in message
    assert (
        f"terraform -chdir=terraform/bench-endpoint force-unlock {_LOCK_ID}" in message
    )


def test_held_lock_on_apply_neither_destroys_nor_unlocks(stack_dir: Path) -> None:
    # An apply that never took the lock changed nothing, and the lock's holder may be a
    # live run: destroying would tear its endpoint down once it lets go, and unlocking
    # would let two runs write one state.
    terraform = _FakeTerraform(locked_on="apply")

    with (
        pytest.raises(BenchEndpointLockedError),
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        pytest.fail("the body must not run without the state lock")

    assert terraform.subcommands() == ["init", "show", "apply"]


def test_held_lock_on_destroy_raises_with_the_lock_id(stack_dir: Path) -> None:
    terraform = _FakeTerraform(locked_on="destroy")

    with (
        pytest.raises(BenchEndpointLockedError, match=_LOCK_ID),
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        pass

    assert terraform.subcommands() == ["init", "show", "apply", "output", "destroy"]


@pytest.mark.parametrize("subcommand", ["apply", "destroy"])
def test_errored_state_is_copied_to_the_state_bucket(
    stack_dir: Path, subcommand: str
) -> None:
    # Terraform dumps a state it changed but could not push to errored.tfstate in its
    # working directory, which the run's temporary copy loses on exit; the copy in the state
    # bucket is what `terraform state push` recovers from.
    terraform = _FakeTerraform(errored_on=subcommand)
    s3 = _FakeS3(["ok"])

    with (
        pytest.raises(RuntimeError, match="failed to push"),
        _open(terraform, s3, stack_dir),
    ):
        pass

    assert s3.puts == {
        ("slipstream-tfstate-x1", _errored_key(subcommand)): _errored_state(
            subcommand
        ).encode()
    }


def test_errored_state_copy_is_named_on_the_error(stack_dir: Path) -> None:
    terraform = _FakeTerraform(errored_on="destroy")

    with (
        pytest.raises(RuntimeError) as excinfo,
        _open(terraform, _FakeS3(["ok"]), stack_dir),
    ):
        pass

    notes = "\n".join(getattr(excinfo.value, "__notes__", []))
    assert f"s3://slipstream-tfstate-x1/{_errored_key('destroy')}" in notes
    assert "terraform state push" in notes


def test_failed_apply_keeps_its_errored_state_beside_the_destroys(
    stack_dir: Path,
) -> None:
    # The apply's dump records resources the remote state never saw, so the destroy that
    # follows must not overwrite it with its own.
    class _BothErrored(_FakeTerraform):
        def __call__(self, args: Sequence[str], env: Mapping[str, str]) -> str:
            if _subcommand(args) == "destroy":
                workdir = Path(args[0].removeprefix("-chdir="))
                (workdir / "errored.tfstate").write_text(
                    _errored_state("destroy"), encoding="utf-8"
                )
                raise RuntimeError("terraform destroy failed to push its state")
            return super().__call__(args, env)

    s3 = _FakeS3(["ok"])

    with (
        pytest.raises(RuntimeError),
        _open(_BothErrored(errored_on="apply"), s3, stack_dir),
    ):
        pytest.fail("the body must not run after a failed apply")

    assert s3.puts == {
        (
            "slipstream-tfstate-x1",
            _errored_key("apply"),
        ): _errored_state("apply").encode(),
        (
            "slipstream-tfstate-x1",
            _errored_key("destroy"),
        ): _errored_state("destroy").encode(),
    }


def test_failed_errored_state_copy_keeps_the_terraform_error(stack_dir: Path) -> None:
    # The copy is a recovery aid: its failure is noted on the terraform error, never raised
    # over it.
    terraform = _FakeTerraform(errored_on="destroy")
    denied = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutObject"
    )

    with (
        pytest.raises(RuntimeError, match="failed to push") as excinfo,
        _open(terraform, _FakeS3(["ok"], put_error=denied), stack_dir),
    ):
        pass

    notes = "\n".join(getattr(excinfo.value, "__notes__", []))
    assert "could not copy errored.tfstate" in notes
    assert "AccessDenied" in notes
    assert "The dump is lost" in notes


def test_uncopied_errored_state_is_never_copied_under_a_later_commands_name(
    stack_dir: Path,
) -> None:
    # The apply's dump fails to upload; the destroy then fails without leaving one, so
    # nothing may land under the destroy's key.
    class _FirstPutDenied(_FakeS3):
        def __init__(self, sentinels: list[str | None]) -> None:
            super().__init__(sentinels)
            self.denied = False

        def put_object(
            self, *, Bucket: str, Key: str, Body: bytes
        ) -> dict[str, object]:
            if not self.denied:
                self.denied = True
                raise ClientError(
                    {"Error": {"Code": "SlowDown", "Message": "retry"}}, "PutObject"
                )
            return super().put_object(Bucket=Bucket, Key=Key, Body=Body)

    class _ApplyErroredDestroyFails(_FakeTerraform):
        def __call__(self, args: Sequence[str], env: Mapping[str, str]) -> str:
            if _subcommand(args) == "destroy":
                raise RuntimeError("terraform destroy failed")
            return super().__call__(args, env)

    s3 = _FirstPutDenied(["ok"])

    with (
        pytest.raises(RuntimeError, match="destroy failed"),
        _open(_ApplyErroredDestroyFails(errored_on="apply"), s3, stack_dir),
    ):
        pytest.fail("the body must not run after a failed apply")

    assert s3.puts == {}


def test_a_resume_keeps_the_errored_state_an_earlier_attempt_left(
    stack_dir: Path,
) -> None:
    # A resume reuses the run id; if its apply also fails to push, its dump must land
    # beside the first attempt's, which may be the only record of resources still billing.
    s3 = _FakeS3(["ok"])
    later = datetime(2026, 11, 6, 16, 2, 0, 0, tzinfo=UTC)

    for clock in (_CLOCK, later):
        with (
            pytest.raises(RuntimeError, match="failed to push"),
            _open(
                _FakeTerraform(errored_on="apply"),
                s3,
                stack_dir,
                wall_clock=lambda clock=clock: clock,
            ),
        ):
            pytest.fail("the body must not run after a failed apply")

    assert set(s3.puts) == {
        ("slipstream-tfstate-x1", _errored_key("apply")),
        ("slipstream-tfstate-x1", _errored_key("apply", "20261106T160200000000Z")),
    }


def test_terraform_output_carries_no_colour_codes(stack_dir: Path) -> None:
    # Terraform's output lands in the flow's logs and error messages, so ANSI colour is off.
    terraform = _FakeTerraform()

    with _open(terraform, _FakeS3(["ok"]), stack_dir):
        pass

    assert all("-no-color" in args for args, _env in terraform.calls)


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

    envs = [
        env
        for args, env in terraform.calls
        if _subcommand(args) in {"apply", "destroy"}
    ]
    assert len(envs) == 2
    assert all(env == _EXPECTED_VARIABLES for env in envs)


def test_api_key_never_appears_in_terraform_argv(stack_dir: Path) -> None:
    # argv is readable by any process that can see the terraform process (ps,
    # /proc/<pid>/cmdline); the environment only by the same user or root.
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
    # The image bakes the providers the lock file pins; init must not rewrite it.
    assert "-lockfile=readonly" in init_args


def test_apply_and_destroy_run_unattended(stack_dir: Path) -> None:
    terraform = _FakeTerraform()

    with _open(terraform, _FakeS3(["ok"]), stack_dir):
        pass

    unattended = [
        args
        for args, _env in terraform.calls
        if _subcommand(args) in {"apply", "destroy"}
    ]
    assert len(unattended) == 2
    assert all({"-auto-approve", "-input=false"} <= set(args) for args in unattended)


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
        return {"output": f"{_INSTANCE_ID}\n", "show": _EMPTY_STATE}.get(
            _subcommand(args), ""
        )

    with open_bench_endpoint(
        terraform=terraform,
        s3_client=_FakeS3(["ok"]),
        stack_dir=stack_dir,
        topology=_TOPOLOGY,
        api_key="sk-secret-key",
        results_bucket="bench-results-x1",
        run_id=_RUN_ID,
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
        _open(
            terraform,
            _FakeS3(
                [None, "failed: pull bench image; see /var/log/cloud-init-output.log"]
            ),
            stack_dir,
        ),
    ):
        pytest.fail("the body must not run on a host that failed to boot")

    assert terraform.subcommands()[-1] == "destroy"


def test_sentinel_timeout_raises_and_destroys(stack_dir: Path) -> None:
    terraform = _FakeTerraform()
    s3 = _FakeS3([None])
    sleeps: list[float] = []

    with (
        pytest.raises(BenchHostBootError, match="did not report ready within 420s"),
        _open(
            terraform,
            s3,
            stack_dir,
            now=_ticking_clock(step_s=60.0),
            sleep=sleeps.append,
        ),
    ):
        pytest.fail("the body must not run on a host that never booted")

    # The deadline is set at t=0 and each poll then reads the clock 60s on, so the seventh
    # poll sees t=420 and raises: seven reads, the poll interval slept between them.
    assert len(s3.reads) == 7
    assert sleeps == [5.0] * 6
    assert terraform.subcommands()[-1] == "destroy"


def test_unrecognised_sentinel_raises_at_once_and_destroys(stack_dir: Path) -> None:
    # The host writes "ok" or "failed: ..." in one put; any other text will not change, so
    # it raises rather than polling to the timeout.
    terraform = _FakeTerraform()

    with (
        pytest.raises(BenchHostBootError, match="unrecognised boot status 'booting'"),
        _open(
            terraform,
            _FakeS3(["booting"]),
            stack_dir,
            sleep=lambda _s: pytest.fail("an unrecognised status must not be retried"),
        ),
    ):
        pytest.fail("the body must not run")

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
            s3_client=_DeniedS3(),
            stack_dir=stack_dir,
            topology=_TOPOLOGY,
            api_key="sk-secret-key",
            results_bucket="bench-results-x1",
            run_id=_RUN_ID,
            sleep=lambda _s: pytest.fail("a denied read must not be retried"),
        ),
    ):
        pytest.fail("the body must not run")

    assert terraform.subcommands()[-1] == "destroy"


def test_empty_instance_id_output_raises_and_destroys(stack_dir: Path) -> None:
    calls: list[str] = []

    def terraform(args: Sequence[str], env: Mapping[str, str]) -> str:
        calls.append(_subcommand(args))
        return _EMPTY_STATE if _subcommand(args) == "show" else ""

    with (
        pytest.raises(BenchHostBootError, match="bench_host_instance_id"),
        open_bench_endpoint(
            terraform=terraform,
            s3_client=_FakeS3(["ok"]),
            stack_dir=stack_dir,
            topology=_TOPOLOGY,
            api_key="sk-secret-key",
            results_bucket="bench-results-x1",
            run_id=_RUN_ID,
            sleep=lambda _s: None,
        ),
    ):
        pytest.fail("the body must not run without a bench host")

    assert calls[-1] == "destroy"


def test_build_terraform_returns_stdout_with_the_extra_environment() -> None:
    # `sh -c` stands in for terraform: proves the extra env reaches the child process.
    run = build_terraform(binary="sh")

    assert run(["-c", 'printf %s "$TF_VAR_vpc_id"'], {"TF_VAR_vpc_id": "vpc-1"}) == (
        "vpc-1"
    )


def test_build_terraform_keeps_the_worker_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The worker's AWS credentials and TF_CLI_CONFIG_FILE reach terraform alongside the
    # stack variables, so the extra env is merged over the worker's, not substituted.
    monkeypatch.setenv("TF_CLI_CONFIG_FILE", "/opt/terraform/provider-mirror.tfrc")
    run = build_terraform(binary="sh")

    assert run(["-c", 'printf %s "$TF_CLI_CONFIG_FILE"'], {"TF_VAR_x": "1"}) == (
        "/opt/terraform/provider-mirror.tfrc"
    )


def test_build_terraform_stack_variables_override_the_worker_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stray TF_VAR_* on the worker must not override the value the session passes.
    monkeypatch.setenv("TF_VAR_vpc_id", "vpc-worker")
    run = build_terraform(binary="sh")

    assert run(["-c", 'printf %s "$TF_VAR_vpc_id"'], {"TF_VAR_vpc_id": "vpc-1"}) == (
        "vpc-1"
    )


def test_build_terraform_error_carries_the_argv_exit_and_stderr() -> None:
    run = build_terraform(binary="sh")

    with pytest.raises(TerraformError) as excinfo:
        run(["-c", "echo boom >&2; exit 3"], {})

    message = str(excinfo.value)
    assert "boom" in message
    assert "exited 3" in message
    assert "-c" in message


def test_build_terraform_error_leaves_out_the_environment() -> None:
    # The environment carries the api-key, so a failure message must not echo it.
    run = build_terraform(binary="sh")

    with pytest.raises(TerraformError) as excinfo:
        run(["-c", "exit 1"], {"TF_VAR_vllm_api_key": "sk-secret-key"})

    assert "sk-secret-key" not in str(excinfo.value)


# The worker environment the eks stack's bench-endpoint-topology ConfigMap sets.
_WORKER_ENV = {
    "BENCH_ENDPOINT_STATE_BUCKET": "slipstream-tfstate-x1",
    "BENCH_ENDPOINT_VPC_ID": "vpc-1",
    "BENCH_ENDPOINT_PUBLIC_SUBNETS": '["subnet-a","subnet-b"]',
    "BENCH_ENDPOINT_NODE_SECURITY_GROUP_ID": "sg-node",
    "BENCH_ENDPOINT_NODE_AUTOSCALING_GROUPS": '["asg-1"]',
    "BENCH_ENDPOINT_INSTANCE_PROFILE": "slipstream-bench-host",
    "PATH": "/usr/bin",
}


def test_read_topology_parses_the_worker_environment() -> None:
    assert read_topology(_WORKER_ENV) == _TOPOLOGY


@pytest.mark.parametrize(
    "name", sorted(key for key in _WORKER_ENV if key.startswith("BENCH_ENDPOINT_"))
)
def test_read_topology_names_a_missing_variable(name: str) -> None:
    environ = {key: value for key, value in _WORKER_ENV.items() if key != name}

    with pytest.raises(BenchEndpointEnvironmentError, match=name):
        read_topology(environ)


def test_read_topology_names_an_empty_variable() -> None:
    environ = {**_WORKER_ENV, "BENCH_ENDPOINT_VPC_ID": ""}

    with pytest.raises(BenchEndpointEnvironmentError, match="BENCH_ENDPOINT_VPC_ID"):
        read_topology(environ)


@pytest.mark.parametrize("value", ["subnet-a", '{"a": 1}', "[1, 2]", "[]"])
def test_read_topology_rejects_a_list_that_is_not_strings(value: str) -> None:
    # An empty list would launch the load balancer into no subnet; the apply would fail
    # minutes in, so read_topology refuses it before any apply.
    environ = {**_WORKER_ENV, "BENCH_ENDPOINT_PUBLIC_SUBNETS": value}

    with pytest.raises(
        BenchEndpointEnvironmentError, match="BENCH_ENDPOINT_PUBLIC_SUBNETS"
    ):
        read_topology(environ)


def test_read_stack_dir_reads_the_baked_stacks_path() -> None:
    environ = {"BENCH_ENDPOINT_STACK_DIR": "/app/terraform/bench-endpoint"}

    assert read_stack_dir(environ) == Path("/app/terraform/bench-endpoint")


@pytest.mark.parametrize("environ", [{}, {"BENCH_ENDPOINT_STACK_DIR": ""}])
def test_read_stack_dir_names_a_missing_or_empty_variable(
    environ: dict[str, str],
) -> None:
    # An empty value would read as Path(""), the working directory, and terraform would
    # init whatever it found there instead of the baked stack.
    with pytest.raises(BenchEndpointEnvironmentError, match="BENCH_ENDPOINT_STACK_DIR"):
        read_stack_dir(environ)


_LOGGER_NAME = "slipstream_bench.orchestration.bench_endpoint"


def _milestones(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER_NAME]


def test_session_logs_its_milestones_in_order(
    stack_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The apply and boot take minutes with no other output, so each step is logged as it
    # starts or ends; the flow surfaces these lines on its run page.
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    with _open(_FakeTerraform(), _FakeS3(["ok"]), stack_dir, now=_ticking_clock(30.0)):
        pass

    milestones = _milestones(caplog)
    assert milestones[0] == (
        "bench endpoint applying "
        "(state s3://slipstream-tfstate-x1/bench-endpoint/terraform.tfstate)"
    )
    assert milestones[1] == (
        f"bench host {_INSTANCE_ID} launched; waiting for its boot sentinel"
    )
    assert re.fullmatch(rf"bench host {_INSTANCE_ID} ready after \d+s", milestones[2])
    assert milestones[3:] == ["bench endpoint destroying", "bench endpoint destroyed"]
    assert not any("sk-secret-key" in m for m in milestones)


def test_session_logs_the_destroy_but_no_ready_when_the_host_fails_to_boot(
    stack_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    with (
        pytest.raises(BenchHostBootError),
        _open(_FakeTerraform(), _FakeS3(["failed: docker"]), stack_dir),
    ):
        pass

    milestones = _milestones(caplog)
    assert not any("ready" in m for m in milestones)
    assert milestones[-2:] == ["bench endpoint destroying", "bench endpoint destroyed"]


def test_session_does_not_log_destroyed_when_the_destroy_fails(
    stack_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    with (
        pytest.raises(RuntimeError, match="terraform destroy failed"),
        _open(_FakeTerraform(fail_on="destroy"), _FakeS3(["ok"]), stack_dir),
    ):
        pass

    milestones = _milestones(caplog)
    assert milestones[-1] == "bench endpoint destroying"


def _destroy_endpoint(terraform: _FakeTerraform, s3: _FakeS3, stack_dir: Path) -> None:
    destroy_bench_endpoint(
        terraform=terraform,
        s3_client=s3,
        stack_dir=stack_dir,
        topology=_TOPOLOGY,
        api_key="sk-secret-key",
        run_id=_RUN_ID,
        wall_clock=lambda: _CLOCK,
    )


def test_destroy_bench_endpoint_inits_and_destroys_whatever_the_state_holds(
    stack_dir: Path,
) -> None:
    # The crash and cancellation hooks run after the run's process is gone, so they start
    # from the stack and the state alone: whatever the killed run left there is destroyed.
    terraform = _FakeTerraform(existing_state=_HELD_STATE)

    _destroy_endpoint(terraform, _FakeS3([]), stack_dir)

    assert terraform.subcommands() == ["init", "destroy"]
    _args, env = terraform.calls[-1]
    assert env["TF_VAR_vllm_api_key"] == "sk-secret-key"
    assert env["TF_VAR_vpc_id"] == "vpc-1"


def test_destroy_bench_endpoint_names_a_held_lock(stack_dir: Path) -> None:
    # A run killed mid-apply leaves its lock; the hook's destroy reports it with the
    # command that releases it rather than unlocking.
    terraform = _FakeTerraform(locked_on="destroy")

    with pytest.raises(BenchEndpointLockedError, match=f"force-unlock {_LOCK_ID}"):
        _destroy_endpoint(terraform, _FakeS3([]), stack_dir)

    assert terraform.subcommands() == ["init", "destroy"]


def test_destroy_bench_endpoint_copies_an_errored_state(stack_dir: Path) -> None:
    s3 = _FakeS3([])

    with pytest.raises(RuntimeError, match="failed to push"):
        _destroy_endpoint(_FakeTerraform(errored_on="destroy"), s3, stack_dir)

    assert s3.puts == {
        ("slipstream-tfstate-x1", _errored_key("destroy")): _errored_state(
            "destroy"
        ).encode()
    }
