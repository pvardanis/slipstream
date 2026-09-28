# Shared Prefect API bring-up for the orchestrated bench recipes (`just bench`,
# `just knob-sweep`). Sourced into a recipe; defines prefect_server_up, which makes a
# Prefect API reachable for the run and exports PREFECT_API_URL so the
# `slipstream-orchestrate` child sees it. If PREFECT_API_URL is already set (a shared or
# AWS-hosted server, future work) it is used as-is; otherwise, if a local server is
# already listening it is reused, else one is started in the background, waited on, and
# stopped by an EXIT trap when the recipe's shell exits. The resumable cell cache lives
# in S3 (ADR-0012), so a fresh local server per run still resumes already-valid cells.
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
  # A server the operator left running is reused rather than started a second time
  # (a second bind on the port would fail).
  if curl -sf "${PREFECT_API_URL}/health" >/dev/null 2>&1; then
    echo "reusing Prefect server at ${PREFECT_API_URL}" >&2
    return 0
  fi
  echo "starting local Prefect server at ${PREFECT_API_URL}..." >&2
  local log=/tmp/prefect-server.log
  uv run --extra orchestration prefect server start --host "${host}" --port "${port}" \
    >"${log}" 2>&1 &
  local pid=$!
  # Stop only the server this run started, whatever the recipe's outcome. A sourced
  # trap sets EXIT for the calling recipe shell, so the server does not outlive the run.
  trap 'kill "'"${pid}"'" 2>/dev/null || true' EXIT
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
