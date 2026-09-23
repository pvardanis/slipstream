# Local test recipes: the Python suite and the shell smokes for the OTel spine and bench image tagging/build (no cluster).

# Run the slipstream-bench Python test suite (no cluster).
cli-test:
    uv run pytest

# Run the request-ID spine stub against a local collector (real OTLP, no cluster).
obs-test:
    bash test/otel_spine_test.sh

# Assert the bench image's slug and content tags derive from model.yaml as expected (no cluster).
image-tag-test:
    bash test/image-tag-test.sh

# Assert build-image.sh assembles the docker build with the model.yaml args and one -t per ref (docker stubbed, no build).
build-image-test:
    bash test/build-image-test.sh
