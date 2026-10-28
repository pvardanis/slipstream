# Local test recipes: the Python suite and the shell smokes for the OTel spine and its goodput parity, bench image tagging/build, and the repo-variable setter, the bootstrap external ID lookup, the orchestration image check, the Grafana MCP server launcher, the OTel Collector's deploy and delivery checks, and the local Helm chart render check (no cluster).

# Run the slipstream-bench Python test suite (no cluster).
[group('test')]
cli-test:
    uv run pytest

# Run the request-ID spine stub against a local collector (real OTLP, no cluster).
[group('test')]
obs-test:
    bash test/otel_spine_test.sh

# Assert the Collector's slo_met gates equal every bench config's goodput gates (static, no collector).
[group('test')]
goodput-parity-test:
    bash test/goodput-parity-test.sh

# Assert the bench image's slug and content tags derive from model.yaml as expected (no cluster).
[group('test')]
image-tag-test:
    bash test/image-tag-test.sh

# Assert build-image.sh assembles the docker build with the model.yaml args and one -t per ref (docker stubbed, no build).
[group('test')]
build-image-test:
    bash test/build-image-test.sh

# Assert bootstrap.sh passes the Grafana external ID and operator email from the environment or tfvars as is, else reads them from the live role and spend-leak topic, prompts when there is nothing to read, and aborts before the apply on a failed read (terraform and aws stubbed).
[group('test')]
bootstrap-test:
    bash test/bootstrap-test.sh

# Assert gh-vars.sh sets every workflow repo variable from the bootstrap outputs and aborts before any set on a missing one (terraform and gh stubbed).
[group('test')]
gh-vars-test:
    bash test/gh-vars-test.sh

# Assert orchestration-image-check.sh looks up the orchestration :main image from the bootstrap outputs and fails naming the publish step when it is missing (terraform and aws stubbed).
[group('test')]
orchestration-image-check-test:
    bash test/orchestration-image-check-test.sh

# Assert grafana-mcp.sh starts the server read-only at its pinned version with the stack URL, from the environment, tfvars or the grafana stack's state, and the token from the bootstrap secret, and aborts before starting it on a failed read (terraform, aws and uvx stubbed).
[group('test')]
grafana-mcp-test:
    bash test/grafana-mcp-test.sh

# Assert otel-collector-check.sh waits for the Collector's ConfigMap and Secret, shows why a rollout failed, and passes delivery only once both Grafana Cloud exporters sent and neither failed (kubectl, curl and sleep stubbed).
[group('test')]
otel-collector-check-test:
    bash test/otel-collector-check-test.sh

# Lint and render every local Helm chart under terraform/*/charts/ and validate it against the Kubernetes and CRD schemas (needs helm and kubeconform, no cluster).
[group('test')]
chart-render-test:
    bash test/chart-render-test.sh
