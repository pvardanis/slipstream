# Local test recipes: the Python suite and the shell smokes for the OTel spine, bench image tagging/build, and the repo-variable setter (no cluster).

# Run the slipstream-bench Python test suite (no cluster).
[group('test')]
cli-test:
    uv run pytest

# Run the request-ID spine stub against a local collector (real OTLP, no cluster).
[group('test')]
obs-test:
    bash test/otel_spine_test.sh

# Assert the bench image's slug and content tags derive from model.yaml as expected (no cluster).
[group('test')]
image-tag-test:
    bash test/image-tag-test.sh

# Assert build-image.sh assembles the docker build with the model.yaml args and one -t per ref (docker stubbed, no build).
[group('test')]
build-image-test:
    bash test/build-image-test.sh

# Assert gh-vars.sh sets every workflow repo variable from the stack outputs and aborts before any set on a missing one (terraform and gh stubbed).
[group('test')]
gh-vars-test:
    bash test/gh-vars-test.sh
