# Plan-level tests for the in-cluster metrics stack on the eks stack (ADR-0026, #367): the
# kube-prometheus-stack release, its ServiceMonitors for vLLM and Karpenter, and the
# remote_write that sends only the allowlisted series to Grafana Cloud. They run offline —
# the aws, helm, and kubernetes providers are mocked and both remote states overridden —
# so the assertions check the release's values without a cluster. The series actually
# landing in Grafana Cloud, the system node's headroom and the active series count are the
# manual tier: `just stack-up`, then query Grafana Cloud.

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

# The grafana stack's outputs name the hosted Prometheus remote_write pushes to.
override_data {
  target = data.terraform_remote_state.grafana
  values = {
    outputs = {
      prometheus_remote_write_url      = "https://prometheus-prod-24-prod-eu-west-2.grafana.net/api/prom/push"
      prometheus_remote_write_username = "1234567"
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

  # The password is the token ESO syncs into monitoring (external_secrets.tf).
  assert {
    condition     = yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.prometheusSpec.remoteWrite[0].basicAuth.password == { name = "grafana-cloud-write", key = "token" }
    error_message = "The remote_write password must be the token key of the grafana-cloud-write Secret."
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

  # Anything off the list stays local, and the price gauge of any other instance type is
  # dropped. g5Xxlarge catches an unescaped dot in the instance type.
  assert {
    condition = alltrue([
      for s in [
        "vllm:num_requests_running;",
        "vllm:kv_cache_usage_perc_extra;",
        "container_cpu_usage_seconds_total;",
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

run "service_monitors_scrape_vllm_and_karpenter" {
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

  # The Karpenter chart's Service, in the controller release's namespace.
  assert {
    condition = yamldecode(helm_release.kube_prometheus_stack.values[0]).prometheus.additionalServiceMonitors.karpenter == {
      namespaceSelector = { matchNames = [helm_release.karpenter.namespace] }
      selector          = { matchLabels = { "app.kubernetes.io/name" = "karpenter" } }
      endpoints         = [{ port = "http-metrics" }]
    }
    error_message = "The Karpenter ServiceMonitor must scrape the http-metrics port of the karpenter Service in the controller's namespace."
  }
}

# A grafana stack not yet applied, or applied before it published the remote_write
# outputs, fails the plan with the step that fixes it.
run "metrics_stack_rejects_a_grafana_stack_without_remote_write_outputs" {
  command = plan

  override_data {
    target = data.terraform_remote_state.grafana
    values = {
      outputs = {}
    }
  }

  expect_failures = [helm_release.kube_prometheus_stack]
}
