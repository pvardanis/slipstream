# Self-hosted Prefect server lifecycle on the cluster. The server itself is a
# helm_release in the eks stack (terraform/eks/prefect.tf): it comes up with
# `just cluster-up` and is torn down by `just cluster-down` (ADR-0015 amendment).
# These recipes wrap what lives outside terraform — the process work pool a worker
# polls, and the port-forward that reaches the UI/API with no public endpoint.

# Create the process work pool (concurrency 1) the sweep worker polls. The server
# Deployment is already applied by `just cluster-up` (helm_release, wait=true), so it
# is Ready here; the pool lives in the server's SQLite, not terraform, so it is
# created over a transient port-forward torn down on exit. Idempotent (--overwrite),
# so re-running a partial stack-up is safe. A process pool with concurrency 1 matches
# the serial single-GPU sweep (ADR-0015); a kubernetes pool is the multi-GPU reopen.
[group('prefect')]
prefect-up:
    #!/usr/bin/env bash
    set -euo pipefail
    namespace="$(terraform -chdir={{ eks_dir }} output -raw prefect_namespace)"
    [[ -n "${namespace}" ]] || { echo "eks output prefect_namespace is empty" >&2; exit 1; }
    kubectl -n "${namespace}" rollout status deploy/prefect-server --timeout=120s
    # Reach the API over a transient port-forward (reaped on exit), so pool creation
    # needs no public endpoint.
    source bench/prefect-portforward.sh
    prefect_api_portforward {{ eks_dir }} {{ prefect_port }}
    uv run --extra orchestration prefect work-pool create {{ prefect_work_pool }} --type process --overwrite
    uv run --extra orchestration prefect work-pool set-concurrency-limit {{ prefect_work_pool }} 1

# Remove the work pool ahead of `cluster-down`. The server rides the cluster teardown
# (it is a helm_release in the eks stack), so this only reaps the pool for symmetry —
# a gone cluster has no server and no pool, so probe reachability and skip when it is
# gone, mirroring `gpu-pool-down`. Best-effort: stack-down guards this with `|| true`
# so a failed pool delete never blocks the money-safety-critical `cluster-down`.
[group('prefect')]
prefect-down:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! kubectl cluster-info --request-timeout=5s >/dev/null 2>&1; then
      echo "prefect-down: cluster API unreachable; nothing to remove (already torn down)" >&2
      exit 0
    fi
    # Best-effort: if the server or its API is already gone, there is no pool to
    # remove — cluster-down reaps the server regardless. Reach it over a transient
    # port-forward, skipping quietly (exit 0) if that fails.
    source bench/prefect-portforward.sh
    prefect_api_portforward {{ eks_dir }} {{ prefect_port }} || { echo "prefect-down: Prefect API not reachable; nothing to remove" >&2; exit 0; }
    uv run --extra orchestration prefect work-pool delete {{ prefect_work_pool }} || echo "prefect-down: work-pool delete failed; the pool rides cluster-down regardless" >&2

# Port-forward the Prefect UI/API to localhost and hold it open (Ctrl-C to stop).
# No public endpoint — this is the only way in (ADR-0015 amendment). Visit
# http://localhost:{{ prefect_port }} for the UI.
[group('prefect')]
prefect-ui:
    #!/usr/bin/env bash
    set -euo pipefail
    namespace="$(terraform -chdir={{ eks_dir }} output -raw prefect_namespace)"
    [[ -n "${namespace}" ]] || { echo "eks output prefect_namespace is empty" >&2; exit 1; }
    echo "Prefect UI at http://localhost:{{ prefect_port }} (Ctrl-C to stop)" >&2
    kubectl -n "${namespace}" port-forward svc/prefect-server {{ prefect_port }}:4200
