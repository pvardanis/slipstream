# Transient port-forward to the in-cluster Prefect server, shared by the lifecycle
# recipes (`just prefect-up`, `just prefect-down`, `just prefect-register-sweep`,
# `just prefect-register-kv-ab`) and the flow-dispatch workflow behind sweep-dispatch
# and kv-ab-dispatch. Sourced into the caller's shell; defines:
#
#   prefect_namespace <eks_dir>   prints the server namespace from the eks stack output
#   prefect_api_portforward <namespace> <port>
#                                 forwards svc/prefect-server to a local port, waits for
#                                 the forwarded API to answer, and exports PREFECT_API_URL
#
# The namespace is a parameter of the forward, not resolved inside it, so a caller with
# no terraform state (the CI runner) passes it in directly. The forward is backgrounded
# and reaped by an EXIT trap on the calling shell, so it never outlives the caller. The
# interactive UI (`just prefect-ui`) forwards in the foreground and does not use this.
# Both return non-zero with a message on stderr on failure.
#
# shellcheck shell=bash

prefect_namespace() {
  local eks_dir="$1" namespace
  # Bare assignment so a failed `terraform output` aborts instead of printing an empty
  # namespace. The eks stack is initialised by `just cluster-up`.
  namespace="$(terraform -chdir="${eks_dir}" output -raw prefect_namespace)"
  [[ -n "${namespace}" ]] || {
    echo "eks output prefect_namespace is empty" >&2
    return 1
  }
  printf '%s\n' "${namespace}"
}

prefect_api_portforward() {
  local namespace="$1" port="$2"
  local log="/tmp/prefect-pf.$$.log"
  kubectl -n "${namespace}" port-forward svc/prefect-server "${port}:4200" >"${log}" 2>&1 &
  local pf=$!
  # Reap the forward whatever the caller's outcome; a sourced trap sets EXIT for the
  # calling shell, so the forward does not outlive the run.
  trap 'kill "'"${pf}"'" 2>/dev/null || true' EXIT
  export PREFECT_API_URL="http://127.0.0.1:${port}/api"
  # Wait for the forwarded API to answer, failing fast if the forward died rather than
  # spinning to the deadline against a port nothing is serving. Each probe is capped so
  # a forward that accepts the connection but never answers cannot stall past the
  # deadline.
  local deadline=$((SECONDS + 30))
  until curl -sf --max-time 2 "${PREFECT_API_URL}/health" >/dev/null 2>&1; do
    if ! kill -0 "${pf}" 2>/dev/null; then
      echo "port-forward to svc/prefect-server exited:" >&2
      cat "${log}" >&2
      return 1
    fi
    ((SECONDS < deadline)) || {
      echo "Prefect API not reachable over port-forward within 30s" >&2
      return 1
    }
    sleep 1
  done
}
