"""Tests that the bench-endpoint stack stays inside what the Prefect worker may apply.

The worker will apply terraform/bench-endpoint without IAM create rights, so the stack must
declare no IAM resources: the bench host's role, policies and instance profile live in the
eks stack (terraform/eks/bench_host.tf) and reach this stack as an input variable. The
load balancer admits only the bench host, so no ingress rule on its security group may name
any other source. terraform test can plan the resources a stack declares but cannot list
them, so these properties are read from the stack's files.
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_BENCH_ENDPOINT = _REPO / "terraform" / "bench-endpoint"
_RESOURCE_HEADER = re.compile(
    r'^resource\s+"(?P<type>[^"]+)"\s+"(?P<name>[^"]+)"', re.MULTILINE
)
_MODULE_HEADER = re.compile(r'^module\s+"(?P<name>[^"]+)"', re.MULTILINE)


def _get_bench_endpoint_files() -> list[str]:
    """The text of every .tf file in the bench-endpoint stack, failing if there are none."""
    texts = [
        p.read_text(encoding="utf-8") for p in sorted(_BENCH_ENDPOINT.glob("*.tf"))
    ]
    assert texts, (
        f"no .tf files under {_BENCH_ENDPOINT}; the scans would pass vacuously"
    )
    return texts


def _get_bench_endpoint_resources() -> list[tuple[str, str, str]]:
    """Every (type, name, body) resource block the bench-endpoint stack declares."""
    resources = []
    for text in _get_bench_endpoint_files():
        headers = list(_RESOURCE_HEADER.finditer(text))
        ends = [h.start() for h in headers[1:]] + [len(text)] if headers else []
        for header, end in zip(headers, ends, strict=True):
            resources.append((header["type"], header["name"], text[header.end() : end]))
    return resources


def test_bench_endpoint_stack_declares_no_iam_resources() -> None:
    iam = [
        f"{kind}.{name}"
        for kind, name, _ in _get_bench_endpoint_resources()
        if kind.startswith("aws_iam_")
    ]

    assert iam == [], (
        f"terraform/bench-endpoint declares IAM resources {iam}; the worker applies this "
        "stack without IAM rights, so move them to terraform/eks/bench_host.tf."
    )


def test_bench_endpoint_stack_calls_no_modules() -> None:
    # A module's resources are invisible to the resource scan above, so a module could
    # bring IAM in unseen.
    modules = [
        m["name"]
        for text in _get_bench_endpoint_files()
        for m in _MODULE_HEADER.finditer(text)
    ]

    assert modules == [], (
        f"terraform/bench-endpoint calls modules {modules}, whose resources the no-IAM "
        "scan cannot see; declare the resources directly instead."
    )


def test_load_balancer_admits_only_the_bench_host() -> None:
    resources = _get_bench_endpoint_resources()
    # aws_security_group_rule and inline ingress blocks are the other two ways to open
    # a security group; the stack uses neither, so any instance is a new opening. An
    # inline block would also make the provider revoke alb_from_host on every apply.
    legacy_rules = [n for kind, n, _ in resources if kind == "aws_security_group_rule"]
    (alb_group,) = [
        body
        for kind, name, body in resources
        if kind == "aws_security_group" and name == "alb"
    ]
    assert legacy_rules == [], f"aws_security_group_rule resources {legacy_rules}"
    assert not re.search(r"^\s*ingress\s*\{", alb_group, re.MULTILINE), (
        "aws_security_group.alb carries an inline ingress block"
    )

    alb_ingress = [
        name
        for kind, name, body in resources
        if kind == "aws_vpc_security_group_ingress_rule"
        and re.search(
            r"^\s*security_group_id\s*=\s*aws_security_group\.alb\.id",
            body,
            re.MULTILINE,
        )
    ]

    assert alb_ingress == ["alb_from_host"], (
        f"The load balancer's security group has ingress rules {alb_ingress}; it must "
        "admit only the bench host (alb_from_host), with mTLS and the api-key as the gate."
    )


def _get_string_attribute(path: Path, attribute: str) -> str:
    """The quoted value of the one `attribute = "..."` line in a .tf file."""
    (value,) = re.findall(
        rf'^\s*{attribute}\s*=\s*"([^"]*)"',
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return value


def test_host_secret_grant_matches_the_secret_the_endpoint_creates() -> None:
    endpoint_name = _get_string_attribute(_BENCH_ENDPOINT / "data.tf", "name")
    created_prefix = _get_string_attribute(
        _BENCH_ENDPOINT / "secrets.tf", "name_prefix"
    ).replace("${local.name}", endpoint_name)
    granted_prefix = _get_string_attribute(
        _REPO / "terraform" / "eks" / "bench_host.tf", "bench_client_secret_prefix"
    )

    assert (
        created_prefix == granted_prefix == "${var.cluster_name}-bench-endpoint-client-"
    ), (
        "The bench host's secrets grant (terraform/eks/bench_host.tf) must match the "
        "bench-client secret name prefix terraform/bench-endpoint creates, or the host "
        "cannot read its certificate and api-key at sweep time."
    )
