# Whole-stack orchestration: bring the benchmark stack up or down, the one-shot cloud verification, and the zero-leak money sweep.

# Bring the whole benchmark stack up and leave it running: cluster, GPU node pool,
# GPU replica, and the ephemeral mTLS bench endpoint. Everything `just bench`
# and `just prefix-cache` need — run them against it as often as you like, then
# `just stack-down` at the end of the day to return spend to zero.
[group('orchestrate')]
stack-up: cluster-up gpu-pool-up gpu-deploy bench-endpoint-up

# Tear the whole benchmark stack down, returning GPU and cluster spend to zero,
# then sweep AWS to confirm nothing tagged Project=slipstream is still billing.
# Bootstrap state and the ECR repo survive (see `just cluster-down`). Order matters:
# drop the bench endpoint and scale the GPU replica to zero, then delete the
# NodePool so Karpenter reaps the g5 before `just cluster-down` destroys the VPC it
# lives in. `cluster-down` is gated on `bench-endpoint-down` succeeding: the endpoint's
# load balancer and host sit in the cluster VPC, so destroying the VPC under a live
# endpoint wedges the destroy on a DependencyViolation and orphans a billing load
# balancer. The GPU scale/pool steps are in-cluster and orthogonal, so they run
# regardless to stop the g5. The leak sweep always runs to report whatever survived.
[group('orchestrate')]
stack-down:
    #!/usr/bin/env bash
    set -euo pipefail
    overall=0
    # Capture the region while the eks stack still has outputs; `cluster-down`
    # destroys them, and the sweep needs a region to target afterwards.
    region="$(terraform -chdir={{ eks_dir }} output -raw region 2>/dev/null || true)"
    # Tear the endpoint down first and gate the VPC destroy on it: a failed
    # endpoint teardown leaves the load balancer and host in the VPC, so running
    # cluster-down anyway would wedge on a DependencyViolation and orphan billing.
    if ! just bench-endpoint-down; then
      echo "bench-endpoint-down failed; skipping cluster-down so the VPC destroy does not wedge under a live endpoint. Resolve the endpoint teardown, then rerun stack-down." >&2
      overall=1
    fi
    just gpu-down || true
    just gpu-pool-down || true
    # cluster-down only when the endpoint is gone. A failed cluster-down is escalated —
    # it means resources may remain billing for the sweep to catch.
    if [[ "${overall}" -eq 0 ]] && ! just cluster-down; then overall=1; fi
    if ! just _zero-leak-sweep "${region}"; then overall=1; fi
    exit "${overall}"

# End-to-end cloud verification of the GPU path (ADR-0002 item 4, #92). Real GPU
# spend, hand-triggered. Brings the cluster and GPU replica up, asserts the engine
# serves a well-formed completion, then tears everything down and sweeps AWS for a
# Project=slipstream instance or volume still billing — asserting zero. Teardown
# and the sweep run even when the smoke fails, so a failed check never orbits a
# live g5. Exits non-zero if the smoke failed, teardown failed, or a leftover
# survived. Every fallible step is guarded so `set -e` cannot skip the teardown.
[group('orchestrate')]
cloud-verify:
    #!/usr/bin/env bash
    set -euo pipefail
    overall=0
    region=""

    # Pre-flight: cloud-verify is a one-shot create -> verify -> destroy money check
    # and must start from a clean slate. Against a pre-existing cluster, cluster-up's
    # create becomes a multi-minute node-group roll, and the teardown below would then
    # destroy a cluster the operator did not stand up here. Abort if the eks stack
    # already holds any resource, pointing at `stack-down` to clear it first.
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    terraform -chdir={{ eks_dir }} init -input=false -backend-config="bucket=${bucket}"
    if terraform -chdir={{ eks_dir }} state list 2>/dev/null | grep -q .; then
      echo "eks stack is not empty; cloud-verify expects a clean slate. Run 'just stack-down' first, then rerun." >&2
      exit 1
    fi

    # Bring the path up. On any failure, stop climbing but still fall through to the
    # teardown + sweep below — a partial apply may already be billing.
    if ! just cluster-up; then overall=1; fi
    # Capture the region while the eks stack still has outputs; after `just cluster-down`
    # they are gone. When cluster-up succeeded the region output must be readable — a
    # missing one there is a hard stop, since falling back to the ambient region could
    # sweep a different one and false-green while a g5 bills. In the partial-apply case
    # the fallback is safe and lives in `_zero-leak-sweep`, which resolves an empty
    # region from AWS_REGION / the profile default.
    region="$(terraform -chdir={{ eks_dir }} output -raw region 2>/dev/null || true)"
    if [[ "${overall}" -eq 0 && -z "${region}" ]]; then
      echo "eks region output unavailable after cluster-up; cannot target the sweep" >&2
      overall=1
    fi
    if [[ "${overall}" -eq 0 ]] && ! just gpu-pool-up; then overall=1; fi
    if [[ "${overall}" -eq 0 ]] && ! just gpu-deploy; then overall=1; fi
    if [[ "${overall}" -eq 0 ]] && ! just gpu-completion; then
      echo "gpu smoke failed" >&2
      overall=1
    fi

    # Teardown always runs, in reaping order. Scale the replica to zero and delete
    # the NodePool (Karpenter reaps the g5) before destroying the VPC it sits in;
    # tolerate the scale/pool steps failing so `cluster-down` still runs. A failed `cluster-down`
    # is escalated — it means resources may remain for the sweep to catch.
    just gpu-down || true
    just gpu-pool-down || true
    if ! just cluster-down; then overall=1; fi

    # Money-safety backstop: assert no tagged resource outlived the teardown.
    if ! just _zero-leak-sweep "${region}"; then overall=1; fi

    exit "${overall}"

# Sweep AWS for a Project=slipstream instance or volume still billing after a
# teardown and exit non-zero if any survives (test/zero-leak-sweep.sh, classified
# by `slipstream-bench zero-leak`). The caller passes the region it captured
# before `cluster-down`, since the eks stack has no outputs left to read it from
# afterwards; an empty region is a hard stop, since the sweep cannot be targeted.
_zero-leak-sweep region:
    #!/usr/bin/env bash
    set -euo pipefail
    # The caller passes the region it captured before cluster-down. When teardown
    # already destroyed the eks outputs (an interrupted cluster-up or a partial
    # cluster-down), that capture is empty exactly when the sweep matters most, so
    # fall back to the ambient AWS region — AWS_REGION, then the profile default —
    # rather than skip the money-safety check.
    region="{{ region }}"
    if [[ -z "${region}" ]]; then
      region="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
    fi
    if [[ -z "${region}" ]]; then
      echo "region unavailable (no argument, AWS_REGION, or profile default); cannot confirm zero leftovers" >&2
      exit 1
    fi
    {{ justfile_directory() }}/test/zero-leak-sweep.sh "${region}"
