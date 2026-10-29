# The in-cluster metrics stack (ADR-0026, "Metrics"): kube-prometheus-stack scraping vLLM,
# Karpenter, dcgm-exporter and kube-state-metrics, and remote-writing only the allowlisted
# series to Grafana Cloud, so dashboards and 14 days of history outlive `cluster-down`.
# Like Karpenter and Prefect it rides the cluster's duty-cycle: up with `cluster-up`, gone
# with `cluster-down`.

# The persistent grafana stack's outputs: the hosted Prometheus push endpoint and the
# instance ID remote_write authenticates as, and the Tempo OTLP endpoint and instance ID
# the Collector's endpoints ConfigMap (otel_collector.tf) holds. Read from that stack's
# state because it reads them from the Grafana Cloud API, so nothing is copied by hand.
data "terraform_remote_state" "grafana" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "grafana/terraform.tfstate"
    region = var.region
  }
}

locals {
  # try() turns a grafana stack not yet applied, or applied before it published these
  # outputs, into the release's actionable precondition failure below.
  prometheus_remote_write_url      = try(data.terraform_remote_state.grafana.outputs.prometheus_remote_write_url, "")
  prometheus_remote_write_username = try(data.terraform_remote_state.grafana.outputs.prometheus_remote_write_username, "")

  # The one place the sent series are listed.
  remote_write_allowlist = yamldecode(file("${path.module}/remote-write-allowlist.yaml"))

  # One keep rule over "<__name__>;<instance_type>", one alternative per allowlisted
  # series: each passes with any instance_type, except the price gauge, which passes only
  # with one of the listed instance types. Relabel regexes are RE2 with no negative
  # lookahead, so the price gauge gets its own alternative rather than being excluded
  # afterwards. Prometheus anchors the regex.
  remote_write_price_metric = "karpenter_cloudprovider_instance_type_offering_price_estimate"
  remote_write_price_types  = join("|", [for t in local.remote_write_allowlist.price_instance_types : replace(t, ".", "\\.")])
  remote_write_keep_regex = join("|", [
    for m in local.remote_write_allowlist.metrics :
    m == local.remote_write_price_metric ? "${m};(?:${local.remote_write_price_types})" : "${m};.*"
  ])

  # The Secret the release creates for the remote_write username: the Prometheus CRD reads
  # both basic-auth halves from Secrets. The instance ID is not a secret.
  prometheus_remote_write_username_secret = "grafana-cloud-prometheus-username"

  # Helm values as a map so the test can read the wiring back; encoded to YAML for the
  # release below. Requests sit beside Karpenter, Prefect, ESO and the Collector on the
  # system node's single t3.large; memory limits equal requests so the node's memory is
  # never overcommitted, and CPU is left unlimited.
  kube_prometheus_stack_helm_values = {
    # Dashboards live in Grafana Cloud and the spend-leak alarm is AWS-side (ADR-0025).
    grafana      = { enabled = false }
    alertmanager = { enabled = false }
    # With no Alertmanager the default alerting rules have no receiver, and no recording
    # rule's output is queried in the cluster or allowlisted for Grafana Cloud, so neither
    # is evaluated on the system node.
    defaultRules = { create = false }
    # EKS runs etcd, the scheduler and the controller manager off the nodes, and binds
    # kube-proxy's metrics to localhost, so their targets would only ever be down.
    kubeEtcd              = { enabled = false }
    kubeScheduler         = { enabled = false }
    kubeControllerManager = { enabled = false }
    kubeProxy             = { enabled = false }

    prometheusOperator = {
      resources = {
        requests = { cpu = "50m", memory = "128Mi" }
        limits   = { memory = "128Mi" }
      }
    }
    kube-state-metrics = {
      resources = {
        requests = { cpu = "10m", memory = "64Mi" }
        limits   = { memory = "64Mi" }
      }
    }
    prometheus-node-exporter = {
      resources = {
        requests = { cpu = "10m", memory = "32Mi" }
        limits   = { memory = "32Mi" }
      }
    }

    prometheus = {
      extraSecret = {
        name = local.prometheus_remote_write_username_secret
        data = { username = local.prometheus_remote_write_username }
      }

      # In the same release as the chart's CRDs, which Helm installs before any of it.
      # Every replica behind a Service is a target, so each vLLM series carries its pod.
      additionalServiceMonitors = {
        # Both vLLM Services, the CPU replica's and the GPU replica's (k8s/vllm.yaml,
        # k8s/vllm-gpu.yaml): whichever is deployed is scraped.
        vllm = {
          namespaceSelector = { matchNames = [kubernetes_namespace_v1.slipstream.metadata[0].name] }
          selector          = { matchExpressions = [{ key = "app", operator = "In", values = ["vllm", "vllm-gpu"] }] }
          endpoints         = [{ port = "http", path = "/metrics" }]
        }
        # The Karpenter chart's Service (karpenter.tf).
        karpenter = {
          namespaceSelector = { matchNames = [helm_release.karpenter.namespace] }
          selector          = { matchLabels = { "app.kubernetes.io/name" = "karpenter" } }
          endpoints         = [{ port = "http-metrics" }]
        }
        # The dcgm-exporter chart's Service (dcgm_exporter.tf), labelled with the chart's
        # name and the release's. The exporter labels each GPU's series with the pod,
        # namespace and container using it. The scrape target already carries the
        # exporter's own pod, namespace and container, so Prometheus renames the
        # exporter's to exported_*, and each is copied back before the copies are
        # dropped. A GPU no pod holds has no exported_pod, so its series carry no pod
        # rather than the exporter's.
        dcgm-exporter = {
          namespaceSelector = { matchNames = [helm_release.dcgm_exporter.namespace] }
          selector = { matchLabels = {
            "app.kubernetes.io/name"     = helm_release.dcgm_exporter.chart
            "app.kubernetes.io/instance" = helm_release.dcgm_exporter.name
          } }
          endpoints = [{
            port = "metrics"
            metricRelabelings = concat(
              [for l in ["pod", "namespace", "container"] : {
                sourceLabels = ["exported_${l}"]
                targetLabel  = l
                regex        = "(.*)"
                replacement  = "$1"
                action       = "replace"
              }],
              [{ regex = "exported_(pod|namespace|container)", action = "labeldrop" }],
            )
          }]
        }
        # The OTel Collector's Service (k8s/otel-collector.yaml), on the port serving its
        # own metrics. Only each exporter's sent and failed counts are kept, which show
        # whether Grafana Cloud takes what the Collector sends.
        otel-collector = {
          namespaceSelector = { matchNames = [kubernetes_namespace_v1.slipstream.metadata[0].name] }
          selector          = { matchLabels = { app = "otel-collector" } }
          endpoints = [{
            port = "metrics"
            metricRelabelings = [{
              sourceLabels = ["__name__"]
              regex        = "otelcol_exporter_(sent|send_failed)_(spans|metric_points)"
              action       = "keep"
            }]
          }]
        }
      }

      prometheusSpec = {
        # An emptyDir, so no EBS volume can leak; the history that matters is in Grafana
        # Cloud. Retention by size stays under the volume's limit, so Prometheus trims its
        # oldest blocks rather than being evicted for filling it.
        storageSpec   = { emptyDir = { sizeLimit = "2Gi" } }
        retentionSize = "1GB"

        # The operator's 600s default keeps a terminating pod's namespace waiting 5m
        # before the namespace controller re-checks it, past the provider's 5m wait on
        # the monitoring namespace's delete at cluster-down. 120s still covers
        # remote_write's 1m flush on shutdown; the emptyDir has nothing else to save.
        terminationGracePeriodSeconds = 120

        # Grafana Cloud's Mimir rejects names outside the legacy character set
        # (err-mimir-label-invalid), and Prometheus 3 otherwise scrapes UTF-8 names such as
        # the OTel Collector's server.address. Legacy validation requests underscore-escaped
        # names at scrape (server_address), so nothing Prometheus holds is lost at
        # remote_write. A target that still serves a non-legacy name past its
        # metricRelabelings fails its whole scrape (up=0).
        nameValidationScheme = "Legacy"

        resources = {
          requests = { cpu = "100m", memory = "768Mi" }
          limits   = { memory = "768Mi" }
        }

        remoteWrite = [{
          url = local.prometheus_remote_write_url
          basicAuth = {
            username = { name = local.prometheus_remote_write_username_secret, key = "username" }
            # The Secret ESO syncs into monitoring (external_secrets.tf).
            password = local.grafana_cloud_write_token_secret
          }
          writeRelabelConfigs = [{
            action       = "keep"
            sourceLabels = ["__name__", "instance_type"]
            regex        = local.remote_write_keep_regex
          }]
        }]
      }
    }
  }
}

resource "helm_release" "kube_prometheus_stack" {
  namespace  = kubernetes_namespace_v1.monitoring.metadata[0].name
  name       = "kube-prometheus-stack"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = var.kube_prometheus_stack_chart_version

  # Block apply until the operator, kube-state-metrics and node-exporter are Ready and roll
  # back atomically on failure, so a broken install fails cluster-up loudly. Helm does not
  # wait on the Prometheus pod, which the operator creates: until the write token's Secret
  # exists the operator holds Prometheus back, which shows in the Prometheus resource's
  # status and the operator's logs; a remote_write that cannot authenticate shows only in
  # Prometheus's logs and the empty Grafana Cloud panels.
  wait   = true
  atomic = true

  values = [yamlencode(local.kube_prometheus_stack_helm_values)]

  lifecycle {
    precondition {
      condition     = startswith(local.prometheus_remote_write_url, "https://") && local.prometheus_remote_write_username != ""
      error_message = "The grafana stack has no https prometheus_remote_write_url or no prometheus_remote_write_username output, so Prometheus has nowhere to remote_write. Run `just grafana-up`, then `just cluster-up` again."
    }
  }

  # The write token's ExternalSecret is installed first; Helm does not wait for its sync.
  # module.eks brings the node group the pods need.
  depends_on = [module.eks, helm_release.grafana_cloud_write_token]
}
