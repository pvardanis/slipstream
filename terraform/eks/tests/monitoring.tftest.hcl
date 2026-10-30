# Plan-level tests for the in-cluster metrics stack on the eks stack (ADR-0026, #367, #368):
# the kube-prometheus-stack release, its ServiceMonitors for vLLM, Karpenter,
# dcgm-exporter and the OTel Collector, the dcgm-exporter DaemonSet on the GPU nodes, the remote_write that
# sends only the allowlisted series to Grafana Cloud, and the ConfigMap naming where the
# Collector sends traces and span metrics in Grafana Cloud (#369). They run offline — the
# aws, helm, and kubernetes providers are mocked and both remote states overridden — so
# the assertions check the releases' values without a cluster. The series actually landing in
# Grafana Cloud with the vLLM pod's name, the system node's headroom, the active series
# count, and the GPU node still reaped at scale-to-zero are the manual tier: `just
# stack-up` with a GPU replica, then query Grafana Cloud.

# The eks module builds IAM roles whose assume_role_policy is validated as JSON at plan;
# the mock provider's random string is not valid JSON, so every policy document gets a
# valid (empty) JSON default here. Mirrors karpenter.tftest.hcl.
mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "aws_partition" {
    defaults = {
      partition  = "aws"
      dns_suffix = "amazonaws.com"
    }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["eu-west-1a", "eu-west-1b", "eu-west-1c"] }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111122223333"
      arn        = "arn:aws:iam::111122223333:user/test"
    }
  }
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/test" }
  }
  # The worker layer plans with the stack; its image resolves to one content-sha tag.
  mock_data "aws_ecr_image" {
    defaults = { image_tags = ["main", "orchestrator-abc123"] }
  }
}

# The Karpenter chart is pulled through a second, us-east-1-pinned aws provider (public
# ECR's token API); mocking the default aws does not cover a distinct provider
# configuration, so without this the test makes a real STS call.
mock_provider "aws" {
  alias = "virginia"
}
mock_provider "helm" {}
mock_provider "kubernetes" {}

# The stack reads the bootstrap remote state; stub it instead of reaching for real S3.
override_data {
  target = data.terraform_remote_state.bootstrap
  values = {
    outputs = {
      orchestration_image_repo_url    = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
      sweep_dispatch_role_arn         = "arn:aws:iam::111122223333:role/slipstream-sweep-dispatch"
      cluster_name                    = "slipstream"
      prefect_namespace               = "prefect"
      grafana_cloud_write_secret_name = "grafana-cloud/write"
    }
  }
}

# The grafana stack's outputs name the hosted Prometheus remote_write pushes to and the
# hosted Tempo the Collector exports traces to.
override_data {
  target = data.terraform_remote_state.grafana
  values = {
    outputs = {
      prometheus_remote_write_url      = "https://prometheus-prod-24-prod-eu-west-2.grafana.net/api/prom/push"
      prometheus_remote_write_username = "1234567"
      tempo_otlp_endpoint              = "tempo-prod-10-prod-eu-west-2.grafana.net:443"
      tempo_username                   = "7654321"
    }
  }
}

variables {
  state_bucket = "slipstream-tfstate-abc123"
}

run "metrics_stack_shape" {
  command = plan

  # An exact x.y.z, never a range or an empty string Helm would resolve to the latest
  # chart: a cluster-up then installs the same stack every session.
  assert {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", helm_release.kube_prometheus_stack.version))
    error_message = "The kube-prometheus-stack chart must be pinned to an exact x.y.z version."
  }
  assert {
    condition     = helm_release.kube_prometheus_stack.repository == "https://prometheus-community.github.io/helm-charts" && helm_release.kube_prometheus_stack.chart == "kube-prometheus-stack"
    error_message = "The metrics stack must be prometheus-community's kube-prometheus-stack chart."
  }
  assert {
    condition     = helm_release.kube_prometheus_stack.namespace == "monitoring"
    error_message = "The metrics stack must run in monitoring, where the write token is synced."
  }
  assert {
    condition     = helm_release.kube_prometheus_stack.wait && helm_release.kube_prometheus_stack.atomic
    error_message = "The kube-prometheus-stack release must set wait and atomic."
  }

  # Dashboards live in Grafana Cloud and the spend-leak alarm is AWS-side (ADR-0025), so
  # neither in-cluster Grafana nor Alertmanager runs.
  assert {
    condition     = yamldecode(helm_release.kube_prometheus_stack.values[0]).grafana.enabled == false
    error_message = "The chart's Grafana must be disabled."
  }
  assert {
    condition     = yamldecode(helm_release.kube_prometheus_stack.values[0]).alertmanager.enabled == false
    error_message = "The chart's Alertmanager must be disabled."
  }

  # Local storage is an emptyDir and nothing else, so no EBS volume can outlive the
  # cluster.
  assert {
    condition     = keys(yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.storageSpec) == ["emptyDir"]
    error_message = "Prometheus storage must be an emptyDir, never a volume claim."
  }

  # Retention by size trims blocks before the emptyDir fills, or the kubelet evicts the
  # pod for exceeding its sizeLimit.
  assert {
    condition = (
      tonumber(regex("^([0-9]+)GB$", yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.retentionSize)[0]) * 1000000000
      < tonumber(regex("^([0-9]+)Gi$", yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.storageSpec.emptyDir.sizeLimit)[0]) * pow(2, 30)
    )
    error_message = "Prometheus retentionSize (in GB) must be below the emptyDir sizeLimit (in Gi)."
  }

  # The namespace controller re-checks a namespace no sooner than its pods' remaining grace
  # period allows, capped at 5m, which is also how long the provider waits on the
  # monitoring namespace's delete at cluster-down. Above remote_write's 1m shutdown flush,
  # so the last samples still reach Grafana Cloud.
  assert {
    condition = (
      try(yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.terminationGracePeriodSeconds, 600) > 60
      && try(yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.terminationGracePeriodSeconds, 600) < 300
    )
    error_message = "Prometheus terminationGracePeriodSeconds must sit between remote_write's 60s flush and the 300s namespace delete timeout."
  }

  # kube-state-metrics emits the allowlisted kube_deployment_* series, and node-exporter
  # is part of the stack #367 specifies. The chart enables both by default, under its
  # top-level toggles and the subcharts' own.
  assert {
    condition = (
      try(yamldecode(helm_release.kube_prometheus_stack.values[0]).kube-state-metrics.enabled, true)
      && try(yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus-node-exporter.enabled, true)
      && try(yamldecode(helm_release.kube_prometheus_stack.values[0]).kubeStateMetrics.enabled, true)
      && try(yamldecode(helm_release.kube_prometheus_stack.values[0]).nodeExporter.enabled, true)
    )
    error_message = "kube-state-metrics and node-exporter must stay enabled."
  }

  # The kubelet's cAdvisor endpoint emits container_cpu_usage_seconds_total, the vLLM pod
  # CPU panel's series. The chart scrapes it by default.
  assert {
    condition = (
      try(yamldecode(helm_release.kube_prometheus_stack.values[0]).kubelet.enabled, true)
      && try(yamldecode(helm_release.kube_prometheus_stack.values[0]).kubelet.serviceMonitor.cAdvisor, true)
    )
    error_message = "The kubelet's cAdvisor endpoint must stay scraped."
  }

  # The chart's default cAdvisor relabelling drops cgroups with no pod, which keeps the
  # cluster-wide container_cpu_usage_seconds_total to one series per container and pod.
  # Setting cAdvisorMetricRelabelings replaces that default.
  assert {
    condition     = try(yamldecode(helm_release.kube_prometheus_stack.values[0]).kubelet.serviceMonitor.cAdvisorMetricRelabelings, null) == null
    error_message = "kubelet.serviceMonitor.cAdvisorMetricRelabelings must stay unset, so the chart's default drops cgroups with no pod before they reach Grafana Cloud."
  }

  # Nothing Prometheus scrapes emits exemplars (ADR-0026).
  assert {
    condition     = !contains(try(yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.enableFeatures, []), "exemplar-storage")
    error_message = "Prometheus must run without the exemplar-storage feature flag."
  }
}

run "remote_write_targets_grafana_cloud_with_the_write_token" {
  command = plan

  # One remote_write, to the hosted Prometheus the grafana stack reads.
  assert {
    condition     = [for w in yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.remoteWrite : w.url] == ["https://prometheus-prod-24-prod-eu-west-2.grafana.net/api/prom/push"]
    error_message = "Prometheus must remote_write to the grafana stack's prometheus_remote_write_url, and nowhere else."
  }

  # Grafana Cloud's Mimir rejects a series whose metric or label name holds a character
  # outside the legacy set (err-mimir-label-invalid), such as the OTel Collector's
  # server.address. Legacy validation has Prometheus request underscore-escaped names at
  # scrape, and fail the scrape of a target that serves one anyway, so it never holds a
  # series remote_write would lose.
  assert {
    condition     = try(yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.nameValidationScheme, "") == "Legacy"
    error_message = "Prometheus must validate metric and label names with the Legacy scheme, the one Grafana Cloud accepts."
  }

  # The password is the token ESO syncs into monitoring (external_secrets.tf): the Secret
  # name and key the monitoring sync release writes.
  assert {
    condition = yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.remoteWrite[0].basicAuth.password == {
      name = yamldecode(helm_release.grafana_cloud_write_token["monitoring"].values[0]).secretName
      key  = yamldecode(helm_release.grafana_cloud_write_token["monitoring"].values[0]).secretKey
    }
    error_message = "The remote_write password must be the Secret name and key the monitoring sync release writes the token to."
  }

  # The username is the instance ID, from a Secret the release creates: the Prometheus
  # CRD reads both basic-auth halves from Secrets.
  assert {
    condition = (
      yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.remoteWrite[0].basicAuth.username.name == yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.extraSecret.name
      && yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.extraSecret.data[yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.remoteWrite[0].basicAuth.username.key] == "1234567"
    )
    error_message = "The remote_write username must be the grafana stack's prometheus_remote_write_username, read from the release's own Secret."
  }
}

# The allowlist is one keep rule over "<name>;<instance_type>". Prometheus anchors relabel
# regexes, so the checks below anchor it too and run it on sample series.
run "remote_write_keeps_only_the_allowlist" {
  command = plan

  assert {
    condition = [for r in yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.remoteWrite[0].writeRelabelConfigs : {
      action       = r.action
      sourceLabels = r.sourceLabels
    }] == [{ action = "keep", sourceLabels = ["__name__", "instance_type"] }]
    error_message = "remote_write must carry exactly one keep rule over __name__ and instance_type."
  }

  # An empty list joins to an empty regex, which drops every series.
  assert {
    condition     = length(local.remote_write_allowlist.metrics) > 0
    error_message = "remote-write-allowlist.yaml must list at least one metric, or the keep rule drops every series."
  }

  # Every allowlisted series is sent, with or without an instance_type label
  # (karpenter_nodes_allocatable carries one); the price gauge only for the GPU pool's
  # instance type.
  assert {
    condition = alltrue(flatten([
      for m in yamldecode(file("remote-write-allowlist.yaml")).metrics : [
        for s in(m == "karpenter_cloudprovider_instance_type_offering_price_estimate" ? ["${m};g5.xlarge"] : ["${m};", "${m};g5.xlarge"]) :
        can(regex("^(?:${yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.remoteWrite[0].writeRelabelConfigs[0].regex})$", s))
      ]
    ]))
    error_message = "Every series in remote-write-allowlist.yaml must pass the remote_write keep rule, with or without an instance_type."
  }

  # The GPU panels' two series (ADR-0026, "Panels: GPU"): busy time and memory-bandwidth
  # active, which carry no instance_type.
  assert {
    condition = alltrue([
      for s in ["DCGM_FI_DEV_GPU_UTIL;", "DCGM_FI_PROF_DRAM_ACTIVE;"] :
      can(regex("^(?:${yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.remoteWrite[0].writeRelabelConfigs[0].regex})$", s))
    ])
    error_message = "The remote_write keep rule must send DCGM_FI_DEV_GPU_UTIL and DCGM_FI_PROF_DRAM_ACTIVE."
  }

  # The vLLM pod CPU panel's series (ADR-0026, the #436 amendment), sent cluster-wide with
  # no label filter: cAdvisor's per-container CPU time carries no instance_type.
  assert {
    condition     = can(regex("^(?:${yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.remoteWrite[0].writeRelabelConfigs[0].regex})$", "container_cpu_usage_seconds_total;"))
    error_message = "The remote_write keep rule must send container_cpu_usage_seconds_total."
  }

  # The Collector's sent and failed counts for each export, so Grafana Cloud shows when it
  # stops taking what the Collector sends.
  assert {
    condition = alltrue([
      for s in [
        "otelcol_exporter_sent_spans;",
        "otelcol_exporter_send_failed_spans;",
        "otelcol_exporter_sent_metric_points;",
        "otelcol_exporter_send_failed_metric_points;",
      ] :
      can(regex("^(?:${yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.remoteWrite[0].writeRelabelConfigs[0].regex})$", s))
    ])
    error_message = "The remote_write keep rule must send the Collector's otelcol_exporter_sent_* and otelcol_exporter_send_failed_* spans and metric points."
  }

  # Anything off the list stays local, and the price gauge of any other instance type is
  # dropped. g5Xxlarge catches an unescaped dot in the instance type.
  assert {
    condition = alltrue([
      for s in [
        "vllm:num_requests_running;",
        "DCGM_FI_PROF_GR_ENGINE_ACTIVE;",
        "vllm:kv_cache_usage_perc_extra;",
        "container_memory_working_set_bytes;",
        "karpenter_cloudprovider_instance_type_offering_price_estimate;g5.2xlarge",
        "karpenter_cloudprovider_instance_type_offering_price_estimate;g5Xxlarge",
        "karpenter_cloudprovider_instance_type_offering_price_estimate;",
      ] :
      !can(regex("^(?:${yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.remoteWrite[0].writeRelabelConfigs[0].regex})$", s))
    ])
    error_message = "The remote_write keep rule must drop every series off the allowlist and the price gauge of other instance types."
  }

  # The price filter names the instance types the GPU NodePool can launch, so the
  # scoreboard's price is never filtered away.
  assert {
    condition     = local.remote_write_allowlist.price_instance_types == jsondecode(regex("node\\.kubernetes\\.io/instance-type\\s+operator:\\s+In\\s+values:\\s+(\\[[^]]*\\])", file("../../k8s/gpu-node-pool.yaml"))[0])
    error_message = "price_instance_types in remote-write-allowlist.yaml must equal the GPU NodePool's instance types (k8s/gpu-node-pool.yaml)."
  }
}

run "service_monitors_scrape_vllm_karpenter_dcgm_exporter_and_the_collector" {
  command = plan

  # Both vLLM Services (CPU and GPU replica), so whichever is deployed is scraped; every
  # replica behind the Service is a target, so each series carries its pod.
  assert {
    condition = yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.vllm == {
      namespaceSelector = { matchNames = ["slipstream"] }
      selector          = { matchExpressions = [{ key = "app", operator = "In", values = ["vllm", "vllm-gpu"] }] }
      endpoints         = [{ port = "http", path = "/metrics" }]
    }
    error_message = "The vLLM ServiceMonitor must scrape /metrics on the http port of the vllm and vllm-gpu Services in slipstream."
  }

  # The selector matches the Services the vLLM manifests deploy, so a label, port or
  # namespace renamed on one side only fails here rather than silently stopping the scrape.
  assert {
    condition = alltrue([
      for f in ["../../k8s/vllm.yaml", "../../k8s/vllm-gpu.yaml"] : (
        length([for d in [for doc in split("\n---\n", file(f)) : yamldecode(doc)] : d if d.kind == "Service"]) == 1
        && alltrue([
          for d in [for doc in split("\n---\n", file(f)) : yamldecode(doc)] : (
            contains(yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.vllm.selector.matchExpressions[0].values, d.metadata.labels.app)
            && contains(yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.vllm.namespaceSelector.matchNames, d.metadata.namespace)
            && contains([for p in d.spec.ports : p.name], yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.vllm.endpoints[0].port)
          ) if d.kind == "Service"
        ])
      )
    ])
    error_message = "The vLLM ServiceMonitor's app label, namespace and port must match the Service in k8s/vllm.yaml and in k8s/vllm-gpu.yaml."
  }

  # The Karpenter chart's Service, in the controller release's namespace.
  assert {
    condition = yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.karpenter == {
      namespaceSelector = { matchNames = [helm_release.karpenter.namespace] }
      selector          = { matchLabels = { "app.kubernetes.io/name" = "karpenter" } }
      endpoints         = [{ port = "http-metrics" }]
    }
    error_message = "The Karpenter ServiceMonitor must scrape the http-metrics port of the karpenter Service in the controller's namespace."
  }

  # The dcgm-exporter chart's Service, in its release's namespace, selected by the labels
  # the chart puts on it: app.kubernetes.io/name is the chart's name unless a name
  # override renames it, and instance is the release name.
  assert {
    condition = (
      yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.dcgm-exporter.namespaceSelector == { matchNames = [helm_release.dcgm_exporter.namespace] }
      && yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.dcgm-exporter.selector == {
        matchLabels = { "app.kubernetes.io/name" = helm_release.dcgm_exporter.chart, "app.kubernetes.io/instance" = helm_release.dcgm_exporter.name }
      }
      && length(setintersection(keys(yamldecode(helm_release.dcgm_exporter.values[0])), ["nameOverride", "fullnameOverride"])) == 0
    )
    error_message = "The dcgm-exporter ServiceMonitor must select the dcgm-exporter release's Service in its namespace, by the chart's name with no name override."
  }

  # The exporter labels a GPU's series with the pod, namespace and container using it.
  # Each scrape target already carries the exporter's own pod, namespace and container,
  # so Prometheus renames the exporter's to exported_*. Each is copied back, and only
  # then are the exported_* copies dropped; dropped first, the pod name would be lost.
  assert {
    condition = (
      [for e in yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.dcgm-exporter.endpoints : e.port] == ["metrics"]
      && alltrue([
        for l in ["pod", "namespace", "container"] : contains(
          yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.dcgm-exporter.endpoints[0].metricRelabelings,
          { sourceLabels = ["exported_${l}"], targetLabel = l, regex = "(.*)", replacement = "$1", action = "replace" }
        )
      ])
    )
    error_message = "The dcgm-exporter ServiceMonitor must scrape the metrics port and copy exported_pod, exported_namespace and exported_container back to pod, namespace and container."
  }
  assert {
    condition = alltrue(flatten([
      for r in [for rs in [yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.dcgm-exporter.endpoints[0].metricRelabelings] : rs[length(rs) - 1]] : [
        for l in ["pod", "namespace", "container"] : [
          r.action == "labeldrop",
          can(regex("^(?:${r.regex})$", "exported_${l}")),
          !can(regex("^(?:${r.regex})$", l)),
        ]
      ]
    ]))
    error_message = "The dcgm-exporter ServiceMonitor's last relabeling must drop exported_pod, exported_namespace and exported_container, and keep pod, namespace and container."
  }

  # The Collector's Service (k8s/otel-collector.yaml), on the port serving its own
  # metrics, read against the manifest so a label, port or namespace renamed on one side
  # fails here.
  assert {
    condition = alltrue([
      for d in [for doc in split("\n---\n", file("../../k8s/otel-collector.yaml")) : yamldecode(doc)] : (
        yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.otel-collector.namespaceSelector == { matchNames = [d.metadata.namespace] }
        && yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.otel-collector.selector == { matchLabels = { app = d.metadata.labels.app } }
        && [for e in yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.otel-collector.endpoints : e.port] == ["metrics"]
        && contains([for p in d.spec.ports : p.name], "metrics")
      ) if d.kind == "Service"
    ])
    error_message = "The Collector's ServiceMonitor must select the Service in k8s/otel-collector.yaml by its app label and namespace and scrape its metrics port."
  }

  # Of the Collector's own metrics only the exporters' sent and failed counts are kept,
  # the same series delivery reads from the Collector's :8888.
  assert {
    condition = (
      [for r in yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.otel-collector.endpoints[0].metricRelabelings : {
        action       = r.action
        sourceLabels = r.sourceLabels
      }] == [{ action = "keep", sourceLabels = ["__name__"] }]
      && alltrue([
        for m in [
          "otelcol_exporter_sent_spans",
          "otelcol_exporter_send_failed_spans",
          "otelcol_exporter_sent_metric_points",
          "otelcol_exporter_send_failed_metric_points",
        ] :
        can(regex("^(?:${yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.otel-collector.endpoints[0].metricRelabelings[0].regex})$", m))
      ])
      && alltrue([
        for m in [
          "otelcol_exporter_queue_size",
          "otelcol_exporter_sent_log_records",
          "otelcol_process_cpu_seconds",
          "otelcol_exporter_sent_spans_extra",
        ] :
        !can(regex("^(?:${yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.otel-collector.endpoints[0].metricRelabelings[0].regex})$", m))
      ])
    )
    error_message = "The Collector's ServiceMonitor must keep only otelcol_exporter_sent_* and otelcol_exporter_send_failed_* for spans and metric points."
  }
}

# dcgm-exporter (#368) is one DaemonSet on the GPU nodes only, read against the GPU
# NodePool's own label and taint (k8s/gpu-node-pool.yaml), so a renamed label or taint on
# either side fails here rather than leaving the exporter on no GPU node, or on the wrong nodes.
run "gpu_metrics_exporter_runs_only_on_gpu_nodes" {
  command = plan

  # An exact x.y.z, like every other chart the stack installs.
  assert {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", helm_release.dcgm_exporter.version))
    error_message = "The dcgm-exporter chart must be pinned to an exact x.y.z version."
  }
  assert {
    condition     = helm_release.dcgm_exporter.repository == "https://nvidia.github.io/dcgm-exporter/helm-charts" && helm_release.dcgm_exporter.chart == "dcgm-exporter"
    error_message = "The GPU exporter must be NVIDIA's dcgm-exporter chart."
  }
  assert {
    condition     = helm_release.dcgm_exporter.namespace == "monitoring"
    error_message = "dcgm-exporter must run in monitoring, beside the Prometheus that scrapes it."
  }

  # The nodeSelector is the label the GPU NodePool stamps on its nodes.
  assert {
    condition = yamldecode(helm_release.dcgm_exporter.values[0]).nodeSelector == one([
      for d in [for doc in split("\n---\n", file("../../k8s/gpu-node-pool.yaml")) : yamldecode(doc)] :
      d.spec.template.metadata.labels if d.kind == "NodePool"
    ])
    error_message = "dcgm-exporter's nodeSelector must equal the GPU NodePool's node labels (k8s/gpu-node-pool.yaml)."
  }

  # Each of the GPU NodePool's taints is tolerated, and nothing else is: the chart's
  # default control-plane toleration is replaced, not extended. A pool with no taint
  # fails too, rather than every taint passing vacuously.
  assert {
    condition = length(one([
      for d in [for doc in split("\n---\n", file("../../k8s/gpu-node-pool.yaml")) : yamldecode(doc)] :
      d.spec.template.spec.taints if d.kind == "NodePool"
      ])) > 0 && alltrue([
      for t in one([
        for d in [for doc in split("\n---\n", file("../../k8s/gpu-node-pool.yaml")) : yamldecode(doc)] :
        d.spec.template.spec.taints if d.kind == "NodePool"
      ]) :
      contains(yamldecode(helm_release.dcgm_exporter.values[0]).tolerations, { key = t.key, operator = "Exists", effect = t.effect })
    ])
    error_message = "dcgm-exporter must tolerate every taint of the GPU NodePool (k8s/gpu-node-pool.yaml)."
  }
  assert {
    condition     = length(yamldecode(helm_release.dcgm_exporter.values[0]).tolerations) == 1
    error_message = "dcgm-exporter must tolerate only the GPU taint."
  }

  # Helm merges these values over the chart's, whose default CPU limit survives unless
  # the key is set to null; null is what removes it.
  assert {
    condition = (
      contains(keys(yamldecode(helm_release.dcgm_exporter.values[0]).resources.limits), "cpu")
      && yamldecode(helm_release.dcgm_exporter.values[0]).resources.limits.cpu == null
    )
    error_message = "dcgm-exporter must null the chart's default CPU limit, or the exporter is throttled at 200m."
  }

  # Karpenter does not count DaemonSet pods when it finds a node empty, unless a pod opts
  # out of disruption; the exporter must not, or the GPU node outlives scale-to-zero.
  assert {
    condition     = !contains(keys(lookup(yamldecode(helm_release.dcgm_exporter.values[0]), "podAnnotations", {})), "karpenter.sh/do-not-disrupt")
    error_message = "dcgm-exporter's pods must not carry karpenter.sh/do-not-disrupt, or the GPU node is never reaped."
  }
}

# A grafana stack not yet applied, or applied before it published the remote_write
# outputs, fails the plan with the step that fixes it. A stack not yet applied has no
# Tempo outputs either, so the Collector's endpoints fail with it.
run "metrics_stack_rejects_a_grafana_stack_without_remote_write_outputs" {
  command = plan

  override_data {
    target = data.terraform_remote_state.grafana
    values = {
      outputs = {}
    }
  }

  expect_failures = [helm_release.kube_prometheus_stack, kubernetes_config_map_v1.grafana_cloud_endpoints]
}

# Either output missing on its own fails the plan too: a URL with no instance ID to
# authenticate as is no more usable than no URL.
run "metrics_stack_rejects_a_grafana_stack_without_a_remote_write_username" {
  command = plan

  override_data {
    target = data.terraform_remote_state.grafana
    values = {
      outputs = {
        prometheus_remote_write_url = "https://prometheus-prod-24-prod-eu-west-2.grafana.net/api/prom/push"
        tempo_otlp_endpoint         = "tempo-prod-10-prod-eu-west-2.grafana.net:443"
        tempo_username              = "7654321"
      }
    }
  }

  expect_failures = [helm_release.kube_prometheus_stack]
}

# A data source with no URL publishes a bare "/push", which is not an endpoint.
run "metrics_stack_rejects_a_remote_write_url_that_is_not_https" {
  command = plan

  override_data {
    target = data.terraform_remote_state.grafana
    values = {
      outputs = {
        prometheus_remote_write_url      = "/push"
        prometheus_remote_write_username = "1234567"
        tempo_otlp_endpoint              = "tempo-prod-10-prod-eu-west-2.grafana.net:443"
        tempo_username                   = "7654321"
      }
    }
  }

  expect_failures = [helm_release.kube_prometheus_stack]
}

# The Collector (k8s/otel-collector.yaml) reads where to send from this ConfigMap: traces
# over OTLP to the hosted Tempo and span metrics by remote_write to the hosted Prometheus,
# each authenticating as its own instance ID with the write token.
run "collector_endpoints_name_the_hosted_tempo_and_prometheus" {
  command = plan

  assert {
    condition     = kubernetes_config_map_v1.grafana_cloud_endpoints.metadata[0].namespace == "slipstream"
    error_message = "The Grafana Cloud endpoints ConfigMap must sit in slipstream, beside the Collector."
  }
  assert {
    condition     = kubernetes_config_map_v1.grafana_cloud_endpoints.metadata[0].name == "grafana-cloud-endpoints"
    error_message = "The Collector reads the ConfigMap named grafana-cloud-endpoints."
  }
  assert {
    condition = kubernetes_config_map_v1.grafana_cloud_endpoints.data == tomap({
      traces-endpoint  = "tempo-prod-10-prod-eu-west-2.grafana.net:443"
      traces-username  = "7654321"
      metrics-url      = "https://prometheus-prod-24-prod-eu-west-2.grafana.net/api/prom/push"
      metrics-username = "1234567"
    })
    error_message = "The ConfigMap must hold the grafana stack's Tempo OTLP endpoint and username and Prometheus push URL and username."
  }
}

# A grafana stack applied before it published the Tempo outputs fails the plan with the
# step that fixes it, rather than leaving the Collector an empty trace endpoint.
run "collector_endpoints_reject_a_grafana_stack_without_tempo_outputs" {
  command = plan

  override_data {
    target = data.terraform_remote_state.grafana
    values = {
      outputs = {
        prometheus_remote_write_url      = "https://prometheus-prod-24-prod-eu-west-2.grafana.net/api/prom/push"
        prometheus_remote_write_username = "1234567"
      }
    }
  }

  expect_failures = [kubernetes_config_map_v1.grafana_cloud_endpoints]
}

# Either Tempo output missing on its own fails the plan too.
run "collector_endpoints_reject_a_grafana_stack_without_a_tempo_username" {
  command = plan

  override_data {
    target = data.terraform_remote_state.grafana
    values = {
      outputs = {
        prometheus_remote_write_url      = "https://prometheus-prod-24-prod-eu-west-2.grafana.net/api/prom/push"
        prometheus_remote_write_username = "1234567"
        tempo_otlp_endpoint              = "tempo-prod-10-prod-eu-west-2.grafana.net:443"
      }
    }
  }

  expect_failures = [kubernetes_config_map_v1.grafana_cloud_endpoints]
}
