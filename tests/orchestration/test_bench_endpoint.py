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
    BenchEndpointInUseError,
    BenchEndpointTopology,
    BenchHost,
    BenchHostBootError,
    TerraformError,
    TopologyError,
    build_terraform,
    open_bench_endpoint,
    read_stack_dir,
    read_topology,
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


# `terraform show -json` on a backend holding no state, and on one holding a resource.
_EMPTY_STATE = '{"format_version":"1.0"}\n'
_HELD_STATE = (
    '{"format_version":"1.0","values":{"root_module":{"resources":'
    '[{"address":"aws_lb.bench","mode":"managed"}]}}}\n'
)


class _FakeTerraform:
    """A scripted Terraform: records every call and answers ``output`` with an instance id.

    ``fail_on`` names a subcommand (``init``, ``apply``, ``destroy``) that raises instead of
    succeeding, standing in for a non-zero exit. ``show`` answers with the state the backend
    already holds: none by default, or ``existing_state``.
    """

    def __init__(
        self, *, fail_on: str | None = None, existing_state: str = _EMPTY_STATE
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self._fail_on = fail_on
        self._existing_state = existing_state

    def __call__(self, args: Sequence[str], env: Mapping[str, str]) -> str:
        self.calls.append((list(args), dict(env)))
        subcommand = _subcommand(args)
        if subcommand == self._fail_on:
            raise RuntimeError(f"terraform {subcommand} failed")
        if subcommand == "output":
            return f"{_INSTANCE_ID}\n"
        if subcommand == "show":
            return self._existing_state
        return ""

    def subcommands(self) -> list[str]:
        return [_subcommand(args) for args, _env in self.calls]


def _subcommand(args: Sequence[str]) -> str:
    """Return the first argument that is not a global ``-flag``, the Terraform subcommand."""
    return next(arg for arg in args if not arg.startswith("-"))


class _FakeS3:
    """Serve a scripted sequence of boot sentinels, one per ``get_object``.

    ``None`` stands for the sentinel not written yet (S3's NoSuchKey); the last entry repeats
    if polled past the script. Each body ends in the newline the host's ``<<<`` here-string
    writes (``user-data.sh.tftpl``). The keys read are recorded.
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
):
    return open_bench_endpoint(
        terraform=terraform,
        s3_client=s3,
        stack_dir=stack_dir,
        topology=_TOPOLOGY,
        api_key="sk-secret-key",
        results_bucket="bench-results-x1",
        boot_timeout_s=boot_timeout_s,
        sleep=sleep,
        now=now,
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

    with pytest.raises(TopologyError, match=name):
        read_topology(environ)


def test_read_topology_names_an_empty_variable() -> None:
    environ = {**_WORKER_ENV, "BENCH_ENDPOINT_VPC_ID": ""}

    with pytest.raises(TopologyError, match="BENCH_ENDPOINT_VPC_ID"):
        read_topology(environ)


@pytest.mark.parametrize("value", ["subnet-a", '{"a": 1}', "[1, 2]", "[]"])
def test_read_topology_rejects_a_list_that_is_not_strings(value: str) -> None:
    # An empty list would launch the load balancer into no subnet; the apply would fail
    # minutes in, so read_topology refuses it before any apply.
    environ = {**_WORKER_ENV, "BENCH_ENDPOINT_PUBLIC_SUBNETS": value}

    with pytest.raises(TopologyError, match="BENCH_ENDPOINT_PUBLIC_SUBNETS"):
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
    with pytest.raises(TopologyError, match="BENCH_ENDPOINT_STACK_DIR"):
        read_stack_dir(environ)
