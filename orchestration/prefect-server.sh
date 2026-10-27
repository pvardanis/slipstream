# Shared Prefect API bring-up for the orchestrated bench recipe (`just bench`).
# Sourced into a recipe; defines prefect_server_up, which makes a
# Prefect API reachable for the run and exports PREFECT_API_URL so the
# `slipstream-orchestrate` child sees it. If PREFECT_API_URL is already set — by an
# operator pointing at a shared or AWS-hosted server — it is used as-is; otherwise,
# if a local server is
# already listening it is reused, else one is started in the background, waited on, and
# stopped by an EXIT trap when the recipe's shell exits. The resumable cell cache lives
# in S3 (ADR-0012), so a fresh local server per run still resumes already-valid cells.
# Mid-run liveness is the orchestrator's concern: it fails loud if the API becomes
# unreachable, so this only guarantees a reachable server at bring-up.
#
# shellcheck shell=bash
prefect_server_up() {
  # An operator-provided API (a shared or AWS-hosted server) is used as-is: the run
  # points its client at it and starts nothing local to stop.
  if [[ -n "${PREFECT_API_URL:-}" ]]; then
    echo "using Prefect API at ${PREFECT_API_URL}" >&2
    return 0
  fi
  local host=127.0.0.1 port=4200
  export PREFECT_API_URL="http://${host}:${port}/api"
  # A server already listening (one the operator left running, or a concurrent run's on
  # the shared local port) is reused rather than started a second time — a second bind
  # would fail. Reuse trusts a healthy /health as a compatible Prefect server.
  if curl -sf "${PREFECT_API_URL}/health" >/dev/null 2>&1; then
    echo "reusing Prefect server at ${PREFECT_API_URL}" >&2
    return 0
  fi
  echo "starting local Prefect server at ${PREFECT_API_URL}..." >&2
  # Per-run log name so concurrent runs do not clobber each other's startup log.
  local log="/tmp/prefect-server.$$.log"
  uv run prefect server start --host "${host}" --port "${port}" \
    >"${log}" 2>&1 &
  local pid=$!
  # Stop only the server this run started, whatever the recipe's outcome, reaping its
  # child (the API worker) too so nothing is orphaned on the port. A sourced trap sets
  # EXIT for the calling recipe shell, so the server does not outlive the run.
  trap 'pkill -P "'"${pid}"'" 2>/dev/null; kill "'"${pid}"'" 2>/dev/null || true' EXIT
  # First start runs schema migrations, so allow a generous ceiling.
  local deadline=$((SECONDS + 120))
  while ((SECONDS < deadline)); do
    if curl -sf "${PREFECT_API_URL}/health" >/dev/null 2>&1; then
      echo "Prefect server ready" >&2
      return 0
    fi
    # Fail fast if the server process died (a bad install, a busy port) rather than
    # spinning to the deadline against a URL nothing is serving.
    if ! kill -0 "${pid}" 2>/dev/null; then
      echo "Prefect server exited during startup; see ${log}" >&2
      return 1
    fi
    sleep 2
  done
  echo "Prefect server did not become ready within 120s; see ${log}" >&2
  return 1
}
