#!/usr/bin/env bash
# Unit test for scripts/otel-collector-check.sh: kubectl, curl and sleep are stubbed on PATH,
# so no cluster is read and no wait is slept. It checks that preflight passes only once the
# grafana-cloud-endpoints ConfigMap exists in slipstream and the grafana-cloud-write
# ExternalSecret is Ready, and otherwise fails naming the recipe or command that fixes it;
# and that rollout passes once the Deployment rolls out, and otherwise fails printing each
# Collector pod's waiting reason and message, which name a missing key or Secret.
set -euo pipefail
unset KUBECTL_FAIL

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/scripts/otel-collector-check.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
mkdir -p "${tmp}/bin"

# Stub kubectl: every call's arguments are logged; KUBECTL_FAIL names the one call, by its
# first words after `-n slipstream`, that fails as the real one would.
cat >"${tmp}/bin/kubectl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"${tmp}/kubectl-calls"
[[ "\$1 \$2" == "-n slipstream" ]] || { echo "unexpected call: \$*" >&2; exit 2; }
shift 2
case "\$*" in
  "get configmap grafana-cloud-endpoints -o name")
    [[ "\${KUBECTL_FAIL:-}" != "get configmap" ]] || { echo 'Error from server (NotFound): configmaps "grafana-cloud-endpoints" not found' >&2; exit 1; }
    echo configmap/grafana-cloud-endpoints ;;
  "wait --for=condition=Ready externalsecret/grafana-cloud-write --timeout=120s")
    [[ "\${KUBECTL_FAIL:-}" != "wait" ]] || { echo 'error: timed out waiting for the condition on externalsecrets/grafana-cloud-write' >&2; exit 1; }
    echo externalsecret.external-secrets.io/grafana-cloud-write condition met ;;
  "rollout status deploy/otel-collector --timeout=120s")
    [[ "\${KUBECTL_FAIL:-}" != "rollout" ]] || { echo 'error: timed out waiting for the condition' >&2; exit 1; } ;;
  "get pods -l app=otel-collector -o jsonpath="*)
    echo "otel-collector-7d9f: CreateContainerConfigError couldn't find key traces-endpoint in ConfigMap slipstream/grafana-cloud-endpoints" ;;
  *) echo "unexpected call: -n slipstream \$*" >&2; exit 2 ;;
esac
EOF
chmod +x "${tmp}/bin/"*

fail=0
run() {
  : >"${tmp}/kubectl-calls"
  PATH="${tmp}/bin:${PATH}" bash "${script}" "$@" >"${tmp}/stdout" 2>"${tmp}/stderr"
}
ok() { echo "ok   $1"; }
bad() {
  echo "FAIL $1" >&2
  fail=1
}
# Runs the script with the arguments after the label and expected stderr; it must exit
# non-zero with that text on stderr.
check_fails() {
  local label="$1" want_err="$2"
  shift 2
  if run "$@"; then
    bad "${label}: script exited 0"
  elif ! grep -qF -- "${want_err}" "${tmp}/stderr"; then
    bad "${label}: stderr lacks '${want_err}'"
  else
    ok "${label}"
  fi
}

# Preflight passes with the ConfigMap present and the ExternalSecret Ready.
if run preflight; then ok "preflight passes"; else bad "preflight passes: $(cat "${tmp}/stderr")"; fi
if grep -qxF -- "-n slipstream wait --for=condition=Ready externalsecret/grafana-cloud-write --timeout=120s" "${tmp}/kubectl-calls"; then
  ok "preflight waits for the write token to sync"
else
  bad "preflight waits for the write token to sync"
fi
KUBECTL_FAIL="get configmap" check_fails "missing endpoints ConfigMap" "run \`just cluster-up\`" preflight
if [[ "$(grep -c wait "${tmp}/kubectl-calls")" == 0 ]]; then ok "missing ConfigMap fails before the wait"; else bad "missing ConfigMap fails before the wait"; fi
KUBECTL_FAIL="wait" check_fails "unsynced write token" "kubectl -n slipstream describe externalsecret grafana-cloud-write" preflight
KUBECTL_FAIL="wait" check_fails "unsynced write token names the setup recipe" "just grafana-cloud-setup" preflight
# Rollout passes when the Deployment rolls out, and on a failed one shows why the pod waits.
if run rollout; then ok "rollout passes"; else bad "rollout passes: $(cat "${tmp}/stderr")"; fi
KUBECTL_FAIL="rollout" check_fails "failed rollout shows the pod's waiting reason" \
  "otel-collector-7d9f: CreateContainerConfigError couldn't find key traces-endpoint in ConfigMap slipstream/grafana-cloud-endpoints" rollout
KUBECTL_FAIL="rollout" check_fails "failed rollout names the describe command" "kubectl -n slipstream describe pod -l app=otel-collector" rollout
check_fails "unknown subcommand" "usage: otel-collector-check.sh" bogus

exit "${fail}"
