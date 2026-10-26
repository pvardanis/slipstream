<!-- ADR recording the fleet-health and cost-scoreboard observability design (issue #35): kube-prometheus-stack in the cluster remote-writing an allowlist of series to Grafana Cloud's free tier, so dashboards and 14 days of history outlive every cluster-down; dashboards as code in a persistent terraform/grafana stack; Grafana Cloud tokens kept in Secrets Manager and synced into the cluster by the External Secrets Operator; vLLM's traces carried through the OTel Collector, which renames vLLM's attributes to the spine names, computes per-request goodput, and derives an exemplar-bearing latency histogram, because vLLM's own metrics carry no exemplars; every panel replica-aware (fleet aggregate plus per-pod breakdown, pod name on traces) so horizontal scale needs no panel rewrite; the scoreboard priced from Karpenter's price estimate with no commercial arm. Records why AMP + AMG, self-hosted Grafana, and in-cluster-only storage were rejected. Builds on ADR-0006 (no GPU Operator), ADR-0009 (goodput definition) and the #26 request-ID spine; amends spec §5.1's cut order and §6's scoreboard. -->

# ADR-0026: Fleet observability runs on Grafana Cloud's free tier, with traces through the Collector

- Status: Accepted
- Date: 2026-10-23

## Context

Fleet health and the cost scoreboard (#35) need dashboards and metric history that outlive
the cluster. `just cluster-down` destroys `terraform/eks` at the end of every session, so
anything stored in the cluster is lost, and a "spend stayed at zero after teardown" view can
only come from data outside it. The GPU budget is $100–250 a month, so a few dollars a month
is a real share of it.

Two facts about vLLM v0.29.0 shape the tracing half:

- Its Prometheus histograms carry no exemplars: TTFT and e2e latency are observed with no
  exemplar argument (`vllm/v1/metrics/loggers.py`).
- Its OpenTelemetry tracing (`--otlp-traces-endpoint`, packages shipped in the image) emits
  one `llm_request` span per request. The span carries `gen_ai.request.id` (`chatcmpl-`
  plus the client's `X-Request-Id`, or a random uuid), `gen_ai.latency.time_to_first_token`,
  `gen_ai.latency.e2e` and `gen_ai.usage.{prompt,completion}_tokens`. It does not carry the
  model name, and it honours an incoming W3C `traceparent`.

The #26 spine's Collector drops every span attribute outside its allowlist: `request_id`,
`model_id`, `prompt_tokens`, `completion_tokens` and `prefix_hash`. vLLM's attribute names
match none of them, so with vLLM tracing on, the redaction would strip every vLLM
attribute, `request_id` included.

## Decision

### Metrics: kube-prometheus-stack, remote-writing an allowlist

- **Chart:** `kube-prometheus-stack` from the prometheus-community Helm repo, installed as a
  `helm_release` in `terraform/eks` like Karpenter and Prefect. `just cluster-up` brings
  metrics up from the first minute.
- **What runs:** Prometheus, the Operator, kube-state-metrics and node-exporter. Grafana and
  Alertmanager are off, because dashboards live in Grafana Cloud and the spend-leak alarm is
  AWS-side (ADR-0025). Where SLO burn-rate alerts are evaluated is #41's decision. Local storage is the chart's `emptyDir`, so no EBS volume can leak.
- **Scrape targets:** ServiceMonitors for vLLM, Karpenter and `dcgm-exporter`. The exporter
  runs as one DaemonSet on GPU nodes, keeping ADR-0006's rejection of the GPU Operator. The
  vLLM ServiceMonitor scrapes every replica behind the Service, so each series carries its
  pod. `dcgm-exporter` labels each GPU with the pod using it.
- **Allowlist:** `remote_write` sends only the series the panels read. Everything else stays
  local and dies with the cluster. Karpenter's price gauge has one series per instance type,
  zone and capacity type, so it is filtered to the instance types actually running.
  Adding a panel means adding its series to the allowlist on purpose.

### Backend: Grafana Cloud free

- **What it holds:** Grafana, the metrics store (10k active series, 14-day retention) and
  the traces store (50 GB a month, 14 days). All of it sits outside AWS and costs nothing.
- **Down periods:** while the cluster is down, cluster panels show a gap. A spend panel reads
  CloudWatch's `AWS/Usage` G-class vCPUs and shows them at zero.
- **AWS access:** Grafana Cloud reads CloudWatch by assuming an IAM role in `bootstrap`
  (Grafana Assume Role: external ID, read-only), so no long-lived AWS key exists.
- **Dashboards as code:** dashboards are JSON in the repo, pushed by the Grafana Terraform
  provider from a persistent `terraform/grafana` stack. A dashboard is created, updated or
  deleted when its file is.

### Credentials: Secrets Manager plus the External Secrets Operator

- **Storage:** `bootstrap` creates two empty Secrets Manager secrets:
  - `grafana-cloud/write`: one access-policy token scoped `metrics:write` and
    `traces:write`, used by Prometheus `remote_write` and the Collector's OTLP exporter;
  - `grafana-cloud/api`: the Terraform provider's token.
- **Filling them:** a wizard has the operator mint each token in Grafana Cloud and writes it
  with `aws secretsmanager put-secret-value`, so neither value enters a resource's Terraform
  state.
- **Into the cluster:** the External Secrets Operator, a `helm_release` in `terraform/eks`
  reading through Pod Identity, syncs the write token into the `monitoring` namespace for
  Prometheus and into the `slipstream` namespace for the Collector, since a Secret is read only
  from its own namespace.
- **For dashboards:** `terraform/grafana` reads the API token through a data source. That
  places it in that stack's state, the usual price of a provider credential.

### Traces and `request_id`: through the Collector

- **vLLM side:** the GPU replica sets `--otlp-traces-endpoint` at the Collector, without
  `--collect-detailed-traces`, whose documented performance cost would contaminate bench
  numbers.
- **Replica identity:** vLLM's spans carry only module name and pid as resource attributes,
  so the vLLM pod sets `OTEL_RESOURCE_ATTRIBUTES` with `k8s.pod.name` from the downward API.
  `spanmetrics` carries `k8s.pod.name` as a dimension, so goodput and latency split by
  replica.
- **Lifecycle:** `just stack-up` chains `just obs-up`, so the Collector runs every session.
  Its manifests and `k8s/otel-collector-config.yaml` stay the single source the spine test
  runs.
- **Before `redaction`,** a `transform` processor:
  - renames `gen_ai.request.id` → `request_id`, `gen_ai.usage.prompt_tokens` →
    `prompt_tokens` and `gen_ai.usage.completion_tokens` → `completion_tokens`;
  - strips the `chatcmpl-` prefix, so `request_id` is exactly the client's `X-Request-Id`,
    the value a gateway, Sentry or PostHog stamping the same header joins on;
  - adds `model_id`: `just obs-up` writes `model.yaml`'s `hfId` into the Collector's
    ConfigMap, and the Collector reads it from its environment;
  - sets `slo_met`, below.

  The spine's names and spec §5.1's schema stay canonical.
- **Export and exemplars:** spans go to Grafana Cloud Traces. The `spanmetrics` connector,
  with exemplars on, derives a latency histogram from span duration, which for `llm_request`
  is e2e latency. Its exemplars open the trace, which carries `request_id`.
- **TTFT:** TTFT cannot carry exemplars. A table of the slowest-TTFT requests, a TraceQL
  query on `gen_ai.latency.time_to_first_token` listing `request_id`, answers "which request
  was slow?" for it.
- **No Prometheus exemplar flag:** Prometheus runs without `exemplar-storage`, because
  nothing it scrapes emits exemplars.

### Goodput: per request, the bench's joint gate

- **Definition:** the `transform` sets `slo_met` from each span. A request meets the SLO
  when TTFT ≤ 1.0 s **and** TPOT ≤ 0.05 s, with TPOT = (e2e − TTFT) / (completion_tokens −
  1). `spanmetrics` counts requests by `slo_met`, and live goodput = rate(`slo_met=true`) /
  rate(all).
- **One completion token:** with `completion_tokens` ≤ 1, TPOT is undefined and counts
  as 0, so TTFT alone decides `slo_met`. `vllm bench serve` scores such a request the same
  way, so live and offline goodput agree on it.
- **Why not from histograms:** vLLM's histograms record each gate separately, so they
  cannot give the joint figure ADR-0009 and the bench reports use.
- **Parity:** a static check in CI asserts the Collector's thresholds equal the bench configs'
  `goodput` gates (`["ttft:1000", "tpot:50"]`), so live and offline cannot drift apart.

### Panels

Every serving and GPU panel shows the fleet aggregate (`sum`, or `histogram_quantile` over
`sum by (le)`) beside a per-replica breakdown `by (pod)`, so one hot replica is not hidden in
the average. With one GPU replica the two agree. Under horizontal scale (L2) and
routing (L3) the breakdown shows the imbalance.

- **Replicas:** replicas available against desired, from kube-state-metrics
  (`kube_deployment_status_replicas_available`).

- **GPU:** two utilisation panels.
  - `DCGM_FI_DEV_GPU_UTIL`, labelled busy time.
  - `DCGM_FI_PROF_DRAM_ACTIVE`, labelled memory-bandwidth active. Decode is memory-bound,
    so this panel is the honest one.
- **Serving:** KV-cache usage, `num_requests_waiting`, TTFT p95, the exemplar-bearing e2e
  latency and the slowest-TTFT table.
- **Throughput and goodput:** output tok/s, prompt tok/s, req/s, and live goodput.
- **Karpenter:**
  - node count with create/terminate annotations;
  - pod-provisioning cold start p50/p95;
  - voluntary disruptions by reason.
- **Spend:** the CloudWatch G-vCPU panel.
- **Cost scoreboard:**
  - $/1M tokens: GPU dollars over the dashboard's time range, summed over every GPU node,
    ÷ prompt plus generation tokens served in that range, summed over every replica, × 1e6.
    Idle GPU time is included, so it shows what serving cost.
  - tokens/sec/$.
  - The price is Karpenter's `karpenter_cloudprovider_instance_type_offering_price_estimate`
    for the running GPU node. There is no commercial arm, and no input:output split, since
    that split existed to compare against commercial APIs.

## Alternatives considered

- **AMP + Amazon Managed Grafana in `bootstrap`.** AWS-native, IAM everywhere (no tokens,
  no ESO), up to three years of history. It costs about $9–14 a month ($9 per active
  editor plus ingest past the free 40M samples). AMG sign-in requires IAM Identity Center,
  which requires AWS Organizations, and the account has neither.
- **Self-hosted Grafana OSS on a small EC2 instance, reading AMP.** About $11–20 a month
  with its network path, and an always-on server to patch, against the duty-cycle idea.
- **In-cluster Prometheus and Grafana only.** Free, but history dies at every
  `cluster-down`.
- **Tokens in a local `.env`.** No AWS cost, but secrets outside a manager are not the
  standard pattern.
- **`terraform/eks` writing a `kubernetes_secret` from Secrets Manager.** One controller
  fewer on the system node, but the write token sits in the eks state file.
- **Allowlisting vLLM's `gen_ai.*` names in `redaction`.** These follow the OTel GenAI
  semantic conventions, but the change would rewrite spec §5.1's schema and the spine's
  test contract.
- **A hand-pinned hourly price for the scoreboard.** No pinned value is committed:
  `price_per_hour` lives in per-run YAML. Karpenter's estimate is the price of what is
  actually running, with nothing to keep in sync.
- **Per-gate SLO panels from vLLM's histograms.** Cheaper, but they cannot give the joint
  goodput the bench measures.

## Consequences

- Grafana Cloud is a third-party dependency, and history stops at 14 days.
- The 10k-series cap is a hard limit, and the allowlist is how it is held. Per-replica
  series multiply the allowlist by the replica count. At the handful of GPUs the budget
  allows this stays well under the cap, and the build's series count confirms it.
- vLLM tracing is a serving change. One bench run with tracing on against one with it off
  shows its overhead is noise rather than assuming it.
- Facts the build must verify, not assume:
  - the stack fits on the system node group's single `t3.large`, beside Karpenter, Prefect,
    the External Secrets Operator and the Collector;
  - the A10G supports `DCGM_FI_PROF_*` metrics (`dcgmi profile -l`). If not, the
    memory-bandwidth panel drops;
  - exemplars are enabled on the free Grafana Cloud tenant;
  - Grafana Assume Role works on the free tier;
  - vLLM's metric names match the live `/metrics`.
- The External Secrets Operator is one more controller on the system node.
- Spec §5.1's cut order drops exemplars: they are kept, built through `spanmetrics`. Spec §6's scoreboard drops the commercial arm.
