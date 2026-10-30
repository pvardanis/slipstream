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
    ConfigMap and restarts the Collector, which reads it from its environment at start;
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

- **Definition:** the `transform` sets `slo_met` from each vLLM span, and drops any
  `slo_met` another span arrives with, so only the Collector reports goodput. A request
  meets the SLO when TTFT ≤ 1.0 s **and** TPOT ≤ 0.05 s, with TPOT = (e2e − TTFT) /
  (completion_tokens − 1). `spanmetrics` counts requests by `slo_met`, and live goodput =
  rate(`slo_met=true`) / rate(all).
- **One completion token:** with `completion_tokens` ≤ 1, TPOT is undefined and counts
  as 0, so TTFT alone decides `slo_met`. `vllm bench serve` applies the same one-token
  rule.
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
  - tokens/s per $/h.
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

## Amendment (2026-10-26, #366): how the write token reaches the two namespaces

- **Namespaces:** `terraform/eks` creates `monitoring`. `slipstream` already exists at
  `cluster-up`, created by the same stack (`worker.tf`) for vllm-gpu and the Prefect worker's
  Role, so both ExternalSecrets exist before any consumer is deployed.
- **One store per namespace:** each namespace gets its own `SecretStore` and `ExternalSecret`,
  installed by a local chart (`terraform/eks/charts/grafana-cloud-write-token`) as one Helm
  release per namespace, after the operator's release. There is no `ClusterSecretStore`, so
  no other namespace's `ExternalSecret` can use these stores. A store with no `auth` block
  reads with the controller's own credentials, so permission to create a `SecretStore` is
  what limits who can read the token.
- **The Secret:** `grafana-cloud-write`, key `token`, owned by its `ExternalSecret` and
  refreshed hourly. The operator's role grants `secretsmanager:GetSecretValue` on
  `grafana-cloud/write` only.
- **What `cluster-up` checks:** the operator's webhook rejects a malformed store or
  `ExternalSecret`, failing the apply. Helm does not wait on the sync, so an empty or
  unreadable secret leaves the apply green and shows only in the `ExternalSecret`'s status.

## Amendment (2026-10-28, #369): the send endpoints come from the Cloud API

- **Why:** the hosted Prometheus and Tempo data sources now report their URLs as
  `<host>-local-nlb.internal.grafana.net`, which resolves to a private `10.63.x.x` address.
  Prometheus `remote_write` and the Collector run in AWS and cannot reach it, so the send
  endpoints are not taken from the data sources. The dashboards still query them by UID.
- **Source:** `terraform/grafana` reads the stack through the Grafana Cloud API
  (`data.grafana_cloud_stack`), which reports the public endpoints:
  `prometheus_remote_write_endpoint` and `prometheus_user_id` for metrics, `traces_url` and
  `traces_user_id` for traces. The Tempo OTLP endpoint is the `traces_url` host on port 443.
- **Guard:** each of the four send-endpoint outputs has a precondition: the URL must be a
  public `https://<host>.grafana.net` endpoint and the instance ID must be above 0. An
  internal or missing endpoint fails `just grafana-up`'s plan instead of reaching a sender.
- **A third secret:** reading the Cloud API needs a Cloud access-policy token with
  `stacks:read`, realm this stack. `bootstrap` creates `grafana-cloud/stack-read` empty,
  beside `grafana-cloud/write` and `grafana-cloud/api`, and the setup wizard has the
  operator mint the token in the Cloud Portal and store it. Like the API token, it sits in
  the grafana stack's state as a provider credential.
- **To the Collector:** `terraform/eks` publishes the Tempo endpoint and username and the
  Prometheus push URL and username as the ConfigMap `grafana-cloud-endpoints` in
  `slipstream`. `cluster-up` fails on a precondition when the grafana stack has no Tempo
  outputs.

## Amendment (2026-10-29, #370): the GPU panels' fleet line is an average

- **Why:** both GPU metrics are per-GPU ratios: `DCGM_FI_DEV_GPU_UTIL` a percentage from 0
  to 100, `DCGM_FI_PROF_DRAM_ACTIVE` a fraction from 0 to 1. Their `sum` grows with the GPU
  count and passes 100%, so it does not say how busy the fleet is.
- **Fleet line:** `avg` over the GPUs `slipstream` pods hold. The breakdown is
  `avg by (pod)` over the same GPUs, so with one GPU replica the two agree, as "Panels"
  requires. A GPU no pod holds carries no `pod` label and is in neither.
- **Other panels:** counters and histograms keep `sum`, and `histogram_quantile` over
  `sum by (le)`.

## Amendment (2026-10-29, #370): neither GPU panel tracks load

- **Measured:** a staged load of 1, 4, 16 and 64 concurrent short requests raised output
  from 81 to 2,495 tok/s. Busy time held at 97–99% from one request in flight. Memory-
  bandwidth active held at 0.75 up to 16, then fell to 0.58 at 64. KV-cache usage peaked
  at 5.5%.
- **Busy time:** near 100% whenever any request is in flight, so it shows idle against
  serving, not how much load.
- **Memory-bandwidth active:** each decode step reads the model weights and every running
  sequence's KV cache. Batching shares the weight reads, so with short contexts it falls
  as concurrency rises, and it climbs as long contexts fill the KV cache. It is not "the
  honest one" for load, as "Panels" has it.
- **Load:** read from the Serving and Throughput panels: KV-cache usage, requests waiting
  and output tok/s.

## Amendment (2026-10-30, #371): the TTFT table filters `ttft_ms`, a double

- **Attribute:** the `transform` writes vLLM's TTFT onto the span as `ttft_ms`, in
  milliseconds as spec §5.2's `first_token` event carries it, and redaction keeps it. The
  table queries `ttft_ms`, not `gen_ai.latency.time_to_first_token`, which redaction drops.
  Only the Collector sets `ttft_ms`: a span that is not vLLM's loses any it arrives with.
  This replaces the `gen_ai.latency.time_to_first_token` query in "TTFT" above.
- **Query:** `{ resource.service.name = "vllm" && name = "llm_request" && span.ttft_ms >
  1000.0 } | select(span.request_id, span.ttft_ms)`, the requests over goodput's 1 s TTFT
  gate.
- **Decimal literal:** `ttft_ms` is a double, and Grafana Cloud Tempo matches no double
  against an integer literal. Over the same bench window `span.ttft_ms > 1000` returned no
  traces and `span.ttft_ms > 1000.0` returned 20.
- **Rows:** the panel limits the Tempo trace search to 100 traces, so it lists the first
  100 matches in the window, one request each. The table sorts them by `ttft_ms`, slowest
  first, but they are not the 100 slowest. Where "TTFT" and "Panels" above say
  slowest-TTFT table, read the table of requests over the TTFT gate.
- **Held by CI:** `test/goodput-parity-test.sh` fails unless a Tempo table panel holds that
  filter, piped at most to `select()`, visible and run as TraceQL, with no other visible
  query beside it, its gate a decimal literal equal to the Collector's TTFT gate. Any other
  dashboard query that compares `ttft_ms` must be the same filter.

## Amendment (2026-10-30, #371): e2e latency buckets at most 1.5x wide from 15 s to 120 s

- **Why:** `histogram_quantile` interpolates linearly inside the bucket holding the
  quantile, so the e2e latency p95 can be off by up to that bucket's upper/lower ratio.
  With 2x buckets from 15 s to 120 s (15, 30, 60, 120 s), a bench cell at concurrency 96
  read 106–115 s against the bench's true p95 of 71.0 s, both in the 60–120 s bucket.
- **Buckets:** `spanmetrics` uses 250 ms, 500 ms, 1, 2, 4, 8, 15, 20, 30, 45, 60, 90, 120 and
  300 s. From 15 s to 120 s each bound is at most 1.5 times the one before, so a p95 there is
  off by at most a factor of 1.5. Below 15 s and above 120 s the buckets are wider.
- **Cost:** each span-metric histogram has 15 `_bucket` series, 3 more than with 2x
  bounds. Over a day of bench traffic on 2026-10-29 the duration histogram held 444
  `_bucket` series at 12 per histogram, so the 3 extra bounds add about 111.
- **Held by test:** `just obs-test` reads the bounds the real Collector exports and fails
  unless they span 15 s to 120 s in buckets at most 1.5x wide. It needs Docker and does not
  run in CI.

## Amendment (2026-10-30, #371): the serving panels' sources

- **KV-cache usage:** `vllm:kv_cache_usage_perc` is a per-replica ratio, so like the GPU
  panels its fleet line is `avg`, with `avg by (pod)` beside it.
- **Span metrics:** the Collector's `spanmetrics` connector remote-writes
  `traces_span_metrics_calls_total` and `traces_span_metrics_duration_seconds_bucket` to
  Grafana Cloud itself, bypassing Prometheus and its keep rule. They are listed under
  `collector_metrics` in `terraform/eks/remote-write-allowlist.yaml` so
  `test/dashboard-allowlist-test.sh` accepts the panels reading them; `monitoring.tf` does
  not read that list.

## Amendment (2026-10-30, #371): `request_id` is not the client's `X-Request-Id` on `/v1/completions`

- **Measured:** every `llm_request` span from the bench carries a `request_id` of the form
  `cmpl-<16 hex>-0`.
- **Why:** `/v1/completions`, which the bench calls, names a request `cmpl-` plus the
  `X-Request-Id` or a random id, plus `-<prompt index>`. The `transform` strips only
  `chatcmpl-`, and the bench's `prefix_repetition` dataset sends no `X-Request-Id`. So
  what "Context" and "Traces and `request_id`" say of the request id holds for
  `/v1/chat/completions` only.
- **Effect:** a `request_id` still resolves to one trace, since vLLM's ids are unique, but
  it does not join to a client's logs, Sentry or PostHog. #430 tracks joining it to the
  client's id.

## Amendment (2026-10-30, #372): the Karpenter, spend and scoreboard panels' queries

- **Cold start:** the panel reads `karpenter_pods_provisioning_startup_duration_seconds`,
  a histogram from when Karpenter's scheduling simulation last started placing a pending
  pod until it runs, as p50 and p95 over the dashboard's time range. A simulation that
  finds no capacity for the pod restarts that clock (`MarkPodSchedulingDecisions`), so
  time spent waiting out capacity shortages before the last attempt is left out: on
  2026-10-30 a pod that waited 1,089 s through four g5.xlarge shortages read about 525 s.
  The allowlist sends its `_bucket` series in
  place of `karpenter_pods_startup_duration_seconds`, a summary no panel reads. Karpenter's
  buckets are 2x wide from 150 s to 300 s, so a p95 there can be off by up to a factor
  of 2.
- **Counters Karpenter creates late:** Karpenter's node-created, node-terminated and
  disruption-decision counters and the cold-start histogram are labelled series created at
  their first increment, and `just cluster-up` starts a fresh Karpenter, so `increase()`
  misses each series' first increment, often the session's only one. The range panels take
  `increase()` for a series alive at the range's start and its last value for one born
  inside it. The annotations do the same over a 2-minute window, which always holds both
  30 s scrapes around an increment, so one event can mark two adjacent minutes.
- **Scoreboard dollars:** each minute of the range, every node with an allocatable
  `nvidia.com/gpu` costs the price gauge for its instance type, capacity type and zone,
  divided by 60. A subquery at a one-minute step sums them, so idle GPU minutes count.
  Karpenter reports a node from when it registers, taking its GPU from the NodeClaim
  until the device plugin advertises it, so the minute or so from launch to registration
  is not counted. A teardown sends no staleness marker for the last scrape, so the
  last node is priced for up to Prometheus's 5-minute lookback after it. A hand
  calculation from EC2 node-hours can differ by those minutes. The token count comes from
  `increase()`, which extrapolates to the range's edges at the range's average rate, so
  traffic running up to an edge reads a few percent high: on 2026-10-30, 10 minutes of
  load in an 11-minute range read 1,108,782 tokens against the 1,075,386 the responses
  reported (+3.1%), and +1.0% in a 15-minute range whose edges fell in idle time.
- **Tokens/s per $/h:** tokens served per second over the range divided by GPU dollars per
  hour over the range.
- **Spend:** `AWS/Usage` publishes a datapoint every 5 minutes while a G-class vCPU runs
  and none while none do, so the panel reads 5-minute periods, as the spend-leak alarm
  does, and fills the empty ones with 0 (`FILL`). It reads on-demand vCPUs only, as the GPU NodePool
  launches on-demand capacity only.

## Amendment (2026-10-30, #436): a CPU panel on the vLLM GPU pods

- **Why:** GPU busy time low can mean light traffic or an engine starved by its own CPU
  work: vLLM's engine-core loop schedules, prepares inputs and detokenizes mostly on one
  thread. GPU metrics cannot tell the two apart; the pod's CPU can.
- **Panel:** "vLLM pod CPU (cores)", in the top row beside Replicas, half width each, directly
  above the GPU panels. CPU cores the `vllm` container uses, from
  `rate(container_cpu_usage_seconds_total)`, per `vllm-gpu` pod and summed over the fleet,
  as "Panels" requires; cores are an amount, so the fleet line is a `sum`, not the GPU
  panels' `avg`.
- **Reference lines:**
  - 1 core: the one-thread engine-core loop is CPU-bound there, long before the container
    total reaches allocatable.
  - The pod's CPU request, a constant equal to the request in `k8s/vllm-gpu.yaml`, which a
    test keeps in step.
  - The GPU node's allocatable CPU, from the already-sent `karpenter_nodes_allocatable`
    for the node with an allocatable `nvidia.com/gpu`.
- **No throttling panel:** the pod has no CPU limit, so CFS never throttles it.
- **Sent cluster-wide:** `container_cpu_usage_seconds_total` comes from cAdvisor through
  the kubelet, one series per container and one per pod; the chart's default cAdvisor
  relabelling drops cgroups with no pod, such as the node's own. The allowlist is one keep
  rule over the metric name and `instance_type`; filtering this metric by namespace or
  container would add a second rule for one metric. A system node and a GPU node hold
  tens of pods, so the metric adds at most a few hundred series against the free tier's
  10k, and it is sent with no label filter. The measured count is recorded on #436.
