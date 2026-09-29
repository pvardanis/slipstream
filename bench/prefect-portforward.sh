# Transient port-forward to the in-cluster Prefect server for the lifecycle recipes
# (`just prefect-up`, `just prefect-down`). Sourced into a recipe; defines
# prefect_api_portforward, which resolves the server namespace from the eks stack,
# forwards svc/prefect-server to a local port, waits for the forwarded API to answer,
# and exports PREFECT_API_URL. The forward is backgrounded and reaped by an EXIT trap
# on the calling recipe's shell, so it never outlives the recipe. The interactive UI
# (`just prefect-ui`) forwards in the foreground and does not use this.
#
# Args: $1 eks stack dir, $2 local port. Returns non-zero (with a message on stderr)
# when the server namespace is unresolved or the API does not come up.
#
# shellcheck shell=bash
prefect_api_portforward() {
  local eks_dir="$1" port="$2" namespace
  # Bare assignment so a failed `terraform output` aborts instead of forwarding an
  # empty namespace. The eks stack is initialised by `just cluster-up`.
  namespace="$(terraform -chdir="${eks_dir}" output -raw prefect_namespace)"
  [[ -n "${namespace}" ]] || {
    echo "eks output prefect_namespace is empty" >&2
    return 1
  }
  kubectl -n "${namespace}" port-forward svc/prefect-server "${port}:4200" >"/tmp/prefect-pf.$$.log" 2>&1 &
  local pf=$!
  # Reap the forward whatever the recipe's outcome; a sourced trap sets EXIT for the
  # calling recipe shell, so the forward does not outlive the run.
  trap 'kill "'"${pf}"'" 2>/dev/null || true' EXIT
  export PREFECT_API_URL="http://127.0.0.1:${port}/api"
  # Wait for the forwarded API to answer, failing fast if the forward died rather than
  # spinning to the deadline against a port nothing is serving.
  local deadline=$((SECONDS + 30))
  until curl -sf "${PREFECT_API_URL}/health" >/dev/null 2>&1; do
    if ! kill -0 "${pf}" 2>/dev/null; then
      echo "port-forward to svc/prefect-server exited; see /tmp/prefect-pf.$$.log" >&2
      return 1
    fi
    ((SECONDS < deadline)) || {
      echo "Prefect API not reachable over port-forward within 30s" >&2
      return 1
    }
    sleep 1
  done
}
