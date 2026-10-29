---
name: check-observability
description: QA or debug the fleet's signals in Grafana Cloud through the Grafana MCP. Use when the user says "qa in grafana" or "check the dashboard", after changing the remote_write allowlist, a ServiceMonitor, the OTel Collector config or a dashboard, or when a panel, series, trace or request_id is missing.
context: fork
---
<!-- Checks the fleet's metrics and traces in Grafana Cloud through the read-only Grafana MCP, in a forked subagent so query dumps stay out of the main conversation: QAs a change item by item against an expected result, debugs a missing signal by walking its signal path back to the first dark hop, or pivots from a request_id to its trace. Its one write is `just obs-pivot`'s demo span; Grafana's configuration belongs to the grafana Terraform stack. -->

# check-observability

Target: $ARGUMENTS

The target is one of: a change to QA, a symptom (an empty panel, a missing series or trace), a `request_id`, or "the dashboard". If it is empty, return "no target given: say what to check" and stop.

The Grafana MCP runs with `--disable-write` (`scripts/grafana-mcp.sh`); Grafana's configuration belongs to `terraform/grafana`. The one write this skill makes is `just obs-pivot`'s demo span. Tool names below are the `mcp__grafana__*` tools; they are deferred, so load them with ToolSearch (`select:mcp__grafana__user_info,...`) before concluding they are missing.

## Evidence

- **Lit** and **dark** are judged only from a successful response: a dark signal is a successful query with zero series or traces in a stated window.
- Any MCP error, HTTP error, or non-zero `kubectl`/`curl`/`just` exit makes the item **unverified**: quote the error verbatim and move on. An error is never evidence of a dark hop.
- Every quoted result states its query and its time window.

## Preconditions

- `user_info` succeeds. On any failure, quote it. If the tools are absent, `.mcp.json` runs `just grafana-mcp`, which needs `just grafana-cloud-setup` done; on a 401, the server is most likely holding a token rotated out of the bootstrap secret since it started (`scripts/grafana-mcp.sh` reads it once, at start), so the user restarts the MCP with `/mcp`; a 401 that survives the restart means the secret's token or the stack URL the launcher resolved is wrong, and `just grafana-cloud-setup` replaces them. Then mark every Grafana Cloud item unverified, run only the in-cluster checks under "Debug a missing signal", and stop there.
- `list_datasources` lists both data sources named `grafanacloud-<slug>-prom` and `grafanacloud-<slug>-traces` (their UIDs, `grafanacloud-prom` and `grafanacloud-traces`, carry no slug). Use the UIDs for every query; the stack's other Prometheus data sources (`grafanacloud-usage`, …) hold none of the fleet's series. If either is missing, stop and quote the list.
- `kubectl get nodes` answers. On an error, quote it with `kubectl config current-context`: only an unreachable API server or an unresolvable host means the cluster is down; `Unauthorized` or a wrong context is a finding for the user. While the cluster is down, cluster series have a gap by design (in-cluster Prometheus uses `emptyDir`), so items that need it are unverified, not dark.
- `kubectl -n slipstream get pods -l 'app in (vllm,vllm-gpu)'` lists the vLLM replicas. With none running, vLLM series and traces are dark by design.

## The signal path

Every signal reaches Grafana Cloud through a chain of hops:

- **Scraped metrics:** exporter (vLLM, Karpenter, dcgm-exporter, kube-state-metrics, the Collector's own `otelcol_*` counters) → ServiceMonitor (`terraform/eks/monitoring.tf`) → in-cluster Prometheus (`monitoring` namespace) → keep rule built from `terraform/eks/remote-write-allowlist.yaml` → `remote_write` → `grafanacloud-<slug>-prom`.
- **Traces:** vLLM's `llm_request` span → OTel Collector (`k8s/otel-collector-config.yaml`), which renames `gen_ai.*` to the spine names (`request_id`, `model_id`, `prompt_tokens`, `completion_tokens`) and adds `slo_met` → `grafanacloud-<slug>-traces`.
- **Span metrics:** the Collector's spanmetrics connector derives call counts and an e2e latency histogram from the same spans, split by `slo_met` and `k8s.pod.name` (label `k8s_pod_name`), and writes them straight to `grafanacloud-<slug>-prom` every 60 s through its own `prometheus_remote_write/grafana-cloud` exporter. They never pass through the in-cluster Prometheus or the allowlist.
- **Logs:** the Collector's debug exporter prints each span's `request_id` to its stdout. Grafana Cloud holds no logs from this repo.

Gotchas:

- vLLM's own histograms carry no exemplars; only the span-metrics histogram does. A TTFT panel without exemplars is correct.
- A scraped series missing from the allowlist never reaches Grafana Cloud, by design: the free tier allows 10k active series. The price gauge is also filtered to the allowlist's `price_instance_types`.
- `just obs-pivot` sends one known span (`request_id` `req-demo-001`) as service `obs-pivot`. Exclude `obs-pivot` from any count of vLLM requests.
- vLLM series carry the replica in `pod`, and DCGM series carry the GPU's pod in `pod` too (relabelled in `monitoring.tf`); compare those with the live vLLM pods. Karpenter and kube-state-metrics series carry their own exporter's pod. With one replica running, a per-pod breakdown is untestable: say so.
- Grafana Cloud's Mimir rejects a series whose label names hold a dot (`err-mimir-label-invalid`), and only that series: others in the same `remote_write` stay lit. The in-cluster Prometheus scrapes UTF-8 names and stores dotted labels (`server.address`) that a plain `curl` of the exporter shows escaped (`server_address`), so read a label's name in-cluster, not at the exporter.
- The CloudWatch data source's health check reports its logs half as failing, which is expected (no logs actions are granted).
- Look up metric names with `list_prometheus_metric_names` rather than guessing them.

## In-cluster Prometheus

The MCP sees only Grafana Cloud; the in-cluster hops are read through a port-forward, run in the background and checked before use:

```bash
pf_log="$(mktemp)"
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 19090:9090 >"${pf_log}" 2>&1 &
pf_pid=$!
trap 'kill "${pf_pid}" 2>/dev/null || true; rm -f "${pf_log}"' EXIT
for _ in $(seq 20); do grep -q -- '-> 9090' "${pf_log}" && break; sleep 1; done
grep -q 'Unable to listen' "${pf_log}" && cat "${pf_log}" >&2
curl -fsS -m 10 -G http://localhost:19090/api/v1/query --data-urlencode 'query=<promql>'
```

A response counts only with `"status":"success"`. Metrics outside the allowlist, `prometheus_remote_storage_*` among them, exist only here.

## QA a change

1. List the items. A target that names series, panels or attributes is the item list. Otherwise diff the branch against `origin/main` on the signal-path files above and the dashboard JSON under `terraform/grafana`: every changed series, panel and span attribute is one item. For "the dashboard", the items are every panel of the dashboards `search_dashboards` returns; if it returns none, report "no dashboard provisioned" and QA the series and span attributes directly. Zero items is a stop: "nothing to QA: say what changed".
2. For each item, write its expected result: present or absent, which labels (`pod`, `k8s_pod_name`, `service.name`), and from which replicas.
3. For a change, confirm it is live (a target of named series with no change behind it skips this step and queries the last hour): the Collector's ConfigMap matches `k8s/otel-collector-config.yaml`, the eks or grafana stack's `terraform plan` shows no change, or `list_dashboard_versions` shows the new version. A change not yet live makes every item unverified; say so and stop.
4. Query each item over a window starting at least 2 minutes after the change went live (the pod's `startTime`, or the apply's time), so the span metrics' 60 s flush and `remote_write` have landed: `query_prometheus` for series, `search_tempo_traces` for span attributes. For panels, `get_dashboard_panel_queries`, substitute the dashboard's variables (`$__rate_interval` as four times the 30 s scrape interval), and run each query against the panel's own data source.
5. An item whose result differs from its expected result is dark: debug it as below.

Done when every item has its expected result, its query, window and quoted result, and a verdict of matched, a dark-hop finding, or unverified with its error.

## Debug a missing signal

Walk the signal path backwards from Grafana Cloud until the signal is lit. The finding is the **first dark hop**.

- **Scraped metrics:** query the series in-cluster.
  - Lit in-cluster, dark in Grafana Cloud: check the series name, and for the price gauge its `instance_type`, against the allowlist. If it is listed, the hop is `remote_write`: read `rate(prometheus_remote_storage_samples_failed_total[10m])` and `rate(prometheus_remote_storage_samples_retried_total[10m])` in-cluster, then Prometheus's logs (`kubectl -n monitoring logs prometheus-kube-prometheus-stack-prometheus-0 -c prometheus`), which name a rejected series and its `err-mimir-*` reason.
  - Dark in-cluster: find its target in `/api/v1/targets`. A target that is down is the finding. A target that is absent means its ServiceMonitor is missing: compare `kubectl -n monitoring get servicemonitor` with the chart's `additionalServiceMonitors` in `monitoring.tf`.
- **Collector counters:** an `otelcol_*` series dark in Grafana Cloud is a scraped metric: walk it as above. The Collector's own metrics port shows the counters its scrape drops (it keeps only the exporters' sent and failed counts), and proves delivery when `remote_write` hides it. Port-forward `svc/otel-collector 18888:8888` in `slipstream` the way the in-cluster Prometheus is, then `curl -fsS -m 10 http://localhost:18888/metrics`. A `send_failed` counter absent beside its `sent` counter means no failures yet.
- **Span metrics:** check the traces path is lit first, then read the Collector's `otelcol_exporter_sent_metric_points` and `otelcol_exporter_send_failed_metric_points` for `exporter="prometheus_remote_write/grafana-cloud"`.
- **Traces:** read the Collector's `otelcol_receiver_accepted_spans` (vLLM → Collector), then `otelcol_exporter_sent_spans` and `otelcol_exporter_send_failed_spans` (Collector → Grafana Cloud). Then run `just obs-pivot`: a non-zero exit is dark, with its stderr quoted; after a pass, search Tempo for `req-demo-001`. If `obs-pivot` passes and vLLM's spans are still missing, the hop is vLLM → Collector.

Done when you name the last lit hop and the first dark hop, each backed by a successful response with its query and window.

## Pivot from a request_id

Search Tempo for the span attribute `request_id` (the bare id, `chatcmpl-` stripped), from the request's time if known, otherwise widening up to Tempo's 14-day retention. From the trace, read the pod, latency and `slo_met`, then find the Collector's line for the same id in `kubectl -n slipstream logs -l app=otel-collector --prefix --tail=-1` (and with `--previous` after a restart). A trace not found is "not found in <window>"; a log line missing because the Collector started after the request is inconclusive.

Done when the trace's attributes and its log line are quoted, or each is marked not found or inconclusive with its window.

## Report

Return a summary of the queries' results, not the raw dumps:

- each item with its expected result, its verdict, the query and window that showed it, and a one-line excerpt
- each finding: the last lit hop, the first dark hop, and the evidence for both
- each unverified item with its error
- a link per dashboard or trace, from `generate_deeplink`
