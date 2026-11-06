"""The worker's bench-endpoint environment and a recording bench-endpoint session.

Shared by the knob-sweep and KV cache A/B flow tests: each flow opens its bench endpoint
from the worker's ``BENCH_ENDPOINT_*`` environment and the vLLM api-key Secret, so both set
``WORKER_ENV``, serve ``API_KEY`` from their kubectl fake, and swap the session for a
``RecordingSession`` that yields ``BENCH_HOST_ID`` without applying anything.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from slipstream_bench.orchestration.bench_endpoint import BenchHost

# The bench host the session yields, distinct from any instance id a test passes
# elsewhere, so a cell sent to it can only have come from the session.
BENCH_HOST_ID = "i-0bench"
API_KEY = "sk-bench-123"
STACK_DIR = "/app/terraform/bench-endpoint"

# The worker's environment: the bench-endpoint topology ConfigMap and the image's stack dir.
WORKER_ENV = {
    "BENCH_ENDPOINT_STATE_BUCKET": "slipstream-tfstate-abc123",
    "BENCH_ENDPOINT_VPC_ID": "vpc-0abc",
    "BENCH_ENDPOINT_PUBLIC_SUBNETS": '["subnet-pub-a", "subnet-pub-b"]',
    "BENCH_ENDPOINT_NODE_SECURITY_GROUP_ID": "sg-0node",
    "BENCH_ENDPOINT_NODE_AUTOSCALING_GROUPS": '["eks-cpu-asg-1"]',
    "BENCH_ENDPOINT_INSTANCE_PROFILE": "slipstream-bench-host",
    "BENCH_ENDPOINT_STACK_DIR": STACK_DIR,
}


class RecordingSession:
    """Stand in for the bench-endpoint session: record its arguments, open, and close.

    Yields the bench host the endpoint booted. ``events`` records the open and close, and
    a test that passes its own list places other steps between them. The close raises
    ``destroy_error`` when set.
    """

    def __init__(
        self,
        events: list[str] | None = None,
        destroy_error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.events = [] if events is None else events
        self._destroy_error = destroy_error

    @contextmanager
    def __call__(self, **kwargs: Any) -> Iterator[BenchHost]:
        self.calls.append(kwargs)
        self.events.append("open")
        try:
            yield BenchHost(instance_id=BENCH_HOST_ID)
        finally:
            self.events.append("close")
            if self._destroy_error:
                raise self._destroy_error
