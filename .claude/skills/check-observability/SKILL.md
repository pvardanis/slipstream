---
name: check-observability
description: QA or debug the fleet's signals in Grafana Cloud through the Grafana MCP. Use when the user says "qa in grafana" or "check the dashboard", after changing the remote_write allowlist, a ServiceMonitor, the OTel Collector config or a dashboard, or when a panel, series, trace or request_id is missing.
context: fork
---
<!-- Checks the fleet's metrics and traces in Grafana Cloud through the read-only Grafana MCP, in a forked subagent so query dumps stay out of the main conversation: QAs a change item by item, debugs a missing signal by walking its signal path back to the first dark hop, or pivots from a request_id to its trace. Read-only: changes to Grafana belong to the grafana Terraform stack. -->

# check-observability

Target: $ARGUMENTS

The target is one of: a change to QA, a symptom (an empty panel, a missing series or trace), a `request_id`, or "the dashboard". If it is empty, return "no target given: say what to check" and stop. This skill only reads: the Grafana MCP runs with `--disable-write` (`scripts/grafana-mcp.sh`), and Grafana's configuration belongs to `terraform/grafana`.

## Preconditions

- `mcp__grafana__user_info` answers. If the MCP tools are missing or fail, stop and say so: `.mcp.json` runs `just grafana-mcp`, which needs `just grafana-cloud-setup` done.
- `kubectl get nodes` answers. If the cluster is down, every cluster panel shows a gap by design (in-cluster Prometheus uses `emptyDir` and dies with `just cluster-down`), and only the CloudWatch spend panel and the 14-day history are live. Report that and check only what is still live.

## The signal path

Every signal reaches a panel through a chain of hops. A signal that is present at a hop is **lit**; a missing one is **dark**.

- **Metrics:** exporter (vLLM, Karpenter, dcgm-exporter, kube-state-metrics) → ServiceMonitor (`terraform/eks/monitoring.tf`) → in-cluster Prometheus (`monitoring` namespace) → keep rule built from `terraform/eks/remote-write-allowlist.yaml` → `remote_write` → `grafanacloud-<slug>-prom` → panel.
- **Traces:** vLLM's `llm_request` span → OTel Collector (`k8s/otel-collector-config.yaml`), which renames `gen_ai.*` to the spine names (`request_id`, `model_id`, `prompt_tokens`, `completion_tokens`) and adds `slo_met` → `grafanacloud-<slug>-traces`.
- **Span metrics:** the Collector's spanmetrics connector derives call counts and an e2e latency histogram from the same spans, split by `slo_met` and `k8s.pod.name`, and remote-writes them to the Prometheus data source every 60 s.
- **Logs:** the Collector's debug exporter prints each span's `request_id` to its stdout (`kubectl -n slipstream logs deploy/otel-collector`). Grafana Cloud holds no logs from this repo.

Gotchas:

- vLLM's own histograms carry no exemplars; only the span-metrics histogram does. A TTFT panel without exemplars is correct.
- A series missing from the allowlist never reaches Grafana Cloud, and that is by design: the free tier allows 10k active series. The price gauge is also filtered to the allowlist's `price_instance_types`.
- `just obs-pivot` sends one known span as service `obs-pivot`. Use it to test the Collector → Grafana Cloud hops, and exclude `obs-pivot` from any count of vLLM requests.
- Every panel is replica-aware: check the per-pod breakdown as well as the fleet aggregate.
- A `remote_write` that cannot authenticate shows only in Prometheus's logs and in empty panels. The CloudWatch data source's health check reports its logs half as failing, which is expected (no logs actions are granted).
- Look up metric names with `list_prometheus_metric_names` rather than guessing them; the span metrics' names come from the connector.

## QA a change

1. List the items: diff the branch against `origin/main` on the signal-path files above. Every changed series, panel and span attribute is one item. For "the dashboard", the items are every panel of the dashboards `search_dashboards` returns for the target.
2. For each item, query Grafana Cloud over a window after the change was deployed: `query_prometheus` for series, `search_tempo_traces` for span attributes, and for panels `get_dashboard_panel_queries`, then run each panel's query.
3. If an item comes back empty, debug it as below.

Done when every item has its query and a non-empty result quoted, or a dark-hop finding.

## Debug a missing signal

Walk the signal path backwards from Grafana Cloud until the signal is lit. The finding is the **first dark hop**.

- **Metrics:** port-forward the in-cluster Prometheus (`kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090`) and query the series (`curl -fsS 'http://localhost:9090/api/v1/query?query=<series>'`). If it is lit there and dark in Grafana Cloud, the hop is the allowlist, the keep rule or `remote_write` (check Prometheus's logs). If it is dark there too, check the target's scrape health in `/api/v1/targets`.
- **Traces:** read the Collector's `otelcol_exporter_sent_spans` and `otelcol_exporter_send_failed_spans` in Grafana Cloud and its logs for the `request_id`, then run `just obs-pivot` to prove the Collector → Grafana Cloud hops with a known span.

Done when you name the last lit hop and the first dark hop, each with the command or query you ran and its output.

## Pivot from a request_id

Search Tempo for the span attribute `request_id` (the bare id, with `chatcmpl-` stripped). From the trace, read the pod, latency and `slo_met`, then find the Collector's log line for the same id. If no trace is found, debug the trace path as above.

Done when the trace's attributes and its log line are quoted, or a dark-hop finding.

## Report

Return a summary of the queries' results, not the raw dumps:

- each item as lit or dark, with the query that showed it and a one-line excerpt of the result
- each finding: the last lit hop, the first dark hop, and the evidence for both
- a link per dashboard or trace, from `generate_deeplink`
