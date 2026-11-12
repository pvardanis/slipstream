# Plan-level tests for the Prefect worker's rollout-freeze rights (#514, ADR-0029): a knob
# sweep holds Argo CD off the workloads by adding a deny sync window to the workloads
# AppProject and removing it afterwards, so the worker may get and patch that one
# AppProject in Argo CD's namespace and nothing else there. They run offline — the aws,
# helm, and kubernetes providers are mocked and the remote states are stubbed — so the
# assertions check the grant's shape without a cluster (ADR-0002 tier-1). A merge made
# during a live sweep syncing only after it ends is the live e2e tier.

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


run "worker_rollout_freeze_rights" {
  command = plan

  assert {
    condition     = kubernetes_role_v1.rollout_freeze.metadata[0].namespace == "argocd"
    error_message = "The rollout-freeze Role must live in Argo CD's namespace with the AppProjects."
  }
  assert {
    condition     = length(kubernetes_role_v1.rollout_freeze.rule) == 1 && toset(kubernetes_role_v1.rollout_freeze.rule[0].api_groups) == toset(["argoproj.io"]) && toset(kubernetes_role_v1.rollout_freeze.rule[0].resources) == toset(["appprojects"]) && toset(kubernetes_role_v1.rollout_freeze.rule[0].verbs) == toset(["get", "patch"]) && toset(kubernetes_role_v1.rollout_freeze.rule[0].resource_names) == toset(["workloads"])
    error_message = "The rollout-freeze Role must grant get and patch on the workloads AppProject and nothing else."
  }
  # The Role names the project the Argo CD apps release renders, so renaming that project
  # without the grant fails here rather than at the first sweep.
  assert {
    condition     = alltrue([for name in kubernetes_role_v1.rollout_freeze.rule[0].resource_names : contains(keys(local.argocd_projects), name)])
    error_message = "The rollout-freeze Role must name an AppProject the Argo CD apps release renders."
  }
  assert {
    condition     = kubernetes_role_binding_v1.rollout_freeze.metadata[0].namespace == "argocd" && kubernetes_role_binding_v1.rollout_freeze.role_ref[0].kind == "Role" && kubernetes_role_binding_v1.rollout_freeze.role_ref[0].name == kubernetes_role_v1.rollout_freeze.metadata[0].name
    error_message = "The binding must sit in Argo CD's namespace and reference the rollout-freeze Role, not a ClusterRole."
  }
  assert {
    condition     = length(kubernetes_role_binding_v1.rollout_freeze.subject) == 1 && kubernetes_role_binding_v1.rollout_freeze.subject[0].kind == "ServiceAccount" && kubernetes_role_binding_v1.rollout_freeze.subject[0].name == "prefect-worker" && kubernetes_role_binding_v1.rollout_freeze.subject[0].namespace == "prefect"
    error_message = "The rollout-freeze Role must be bound to the worker's ServiceAccount alone."
  }
}
