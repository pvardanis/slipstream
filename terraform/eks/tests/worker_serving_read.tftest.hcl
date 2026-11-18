# Plan-level tests for the Prefect worker's read on the serving fleet (#571, ADR-0031): the
# bench flows refuse to start while the serving fleet has replicas, so the worker may get
# the serving fleet's Deployment in the serving namespace by name, and nothing else there;
# its slipstream Role keeps its own rules. They run offline (the aws, helm, and kubernetes
# providers are mocked and the remote states are stubbed), so the assertions check the
# grant's shape without a cluster (ADR-0002 tier-1).

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
}

mock_provider "aws" {
  alias = "virginia"
}
mock_provider "helm" {}
mock_provider "kubernetes" {}

override_data {
  target = data.terraform_remote_state.bootstrap
  values = {
    outputs = {
      orchestration_image_repo_url    = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
      orchestration_image_repo_arn    = "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream/orchestration"
      bench_image_repo_arn            = "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream-bench"
      bench_results_bucket_arn        = "arn:aws:s3:::slipstream-bench-results-abc123"
      sweep_dispatch_role_arn         = "arn:aws:iam::111122223333:role/slipstream-sweep-dispatch"
      grafana_cloud_write_secret_name = "grafana-cloud/write"
      cluster_name                    = "slipstream"
      prefect_namespace               = "prefect"
    }
  }
}

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


run "worker_serving_fleet_read" {
  command = plan

  assert {
    condition     = kubernetes_role_v1.serving_fleet_read.metadata[0].namespace == "serving"
    error_message = "The serving fleet read Role must live in the serving namespace."
  }
  # One rule: get on apps Deployments, narrowed to the serving fleet's by name. No list or
  # watch, which resourceNames cannot narrow, so no other workload there is visible.
  assert {
    condition = (
      length(kubernetes_role_v1.serving_fleet_read.rule) == 1
      && toset(kubernetes_role_v1.serving_fleet_read.rule[0].api_groups) == toset(["apps"])
      && toset(kubernetes_role_v1.serving_fleet_read.rule[0].resources) == toset(["deployments"])
      && toset(kubernetes_role_v1.serving_fleet_read.rule[0].verbs) == toset(["get"])
      && toset(kubernetes_role_v1.serving_fleet_read.rule[0].resource_names) == toset(["vllm-serving"])
    )
    error_message = "The serving fleet read Role must grant get on deployment/vllm-serving and nothing else."
  }
  assert {
    condition     = kubernetes_role_binding_v1.serving_fleet_read.metadata[0].namespace == "serving" && kubernetes_role_binding_v1.serving_fleet_read.role_ref[0].kind == "Role" && kubernetes_role_binding_v1.serving_fleet_read.role_ref[0].api_group == "rbac.authorization.k8s.io" && kubernetes_role_binding_v1.serving_fleet_read.role_ref[0].name == kubernetes_role_v1.serving_fleet_read.metadata[0].name
    error_message = "The serving fleet read binding must reference the serving fleet read Role in the serving namespace."
  }
  assert {
    condition     = length(kubernetes_role_binding_v1.serving_fleet_read.subject) == 1 && kubernetes_role_binding_v1.serving_fleet_read.subject[0].kind == "ServiceAccount" && kubernetes_role_binding_v1.serving_fleet_read.subject[0].name == "prefect-worker" && kubernetes_role_binding_v1.serving_fleet_read.subject[0].namespace == "prefect"
    error_message = "The serving fleet read Role must be bound to the worker's ServiceAccount alone."
  }

  # --- The worker's slipstream Role keeps its own five rules ---

  # Each rule as "api_groups|resources|verbs", pinned exactly so no verb or resource is
  # added to it; resource_names is known at plan only where set, so the first rule's is
  # pinned on its own.
  assert {
    condition = [
      for r in kubernetes_role_v1.vllm_gpu_manage.rule : join("|", [
        join(",", sort(r.api_groups)),
        join(",", sort(r.resources)),
        join(",", sort(r.verbs)),
      ])
      ] == [
      "apps|deployments|get,patch",
      "apps|deployments|list,watch",
      "|pods|get,list",
      "|pods/log|get",
      "|events|list",
    ] && toset(kubernetes_role_v1.vllm_gpu_manage.rule[0].resource_names) == toset(["vllm-gpu"])
    error_message = "The worker's slipstream Role must keep its five rules unchanged: the serving fleet read is a separate Role."
  }
}
