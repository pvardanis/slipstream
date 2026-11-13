# Plan-level tests for the Prefect worker's cold-start rights (#538, ADR-0030): the
# cold-start-breakdown flow waits until no GPU NodeClaim remains, then reads the hosting
# NodeClaim's conditions and the namespace's events, so the worker may get, list and
# watch NodeClaims and nodes cluster-wide and list events in the slipstream namespace —
# read-only, nothing else. They run offline — the aws, helm, and kubernetes providers are
# mocked and the remote states are stubbed — so the assertions check the grants' shape
# without a cluster (ADR-0002 tier-1). A dispatched cold start reading them is the live
# e2e tier.

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


run "worker_cold_start_rights" {
  command = plan

  # --- Cluster-scoped: NodeClaims and nodes, read-only ---

  assert {
    condition     = length([for r in kubernetes_cluster_role_v1.cold_start_read.rule : r if toset(r.api_groups) == toset(["karpenter.sh"]) && toset(r.resources) == toset(["nodeclaims"]) && toset(r.verbs) == toset(["get", "list", "watch"])]) == 1
    error_message = "The cold-start ClusterRole must grant get, list and watch on karpenter.sh NodeClaims."
  }
  assert {
    condition     = length([for r in kubernetes_cluster_role_v1.cold_start_read.rule : r if toset(r.api_groups) == toset([""]) && toset(r.resources) == toset(["nodes"]) && toset(r.verbs) == toset(["get", "list", "watch"])]) == 1
    error_message = "The cold-start ClusterRole must grant get, list and watch on nodes."
  }
  # The worker's first cluster-scoped grant stays read-only and names only those two
  # resources, so it can never write to or read beyond them anywhere in the cluster.
  assert {
    condition     = alltrue([for r in kubernetes_cluster_role_v1.cold_start_read.rule : length(setsubtract(toset(r.verbs), toset(["get", "list", "watch"]))) == 0 && length(setsubtract(toset(r.resources), toset(["nodeclaims", "nodes"]))) == 0])
    error_message = "The cold-start ClusterRole must be read-only (get, list, watch) on NodeClaims and nodes alone."
  }
  assert {
    condition     = kubernetes_cluster_role_binding_v1.cold_start_read.role_ref[0].kind == "ClusterRole" && kubernetes_cluster_role_binding_v1.cold_start_read.role_ref[0].name == kubernetes_cluster_role_v1.cold_start_read.metadata[0].name
    error_message = "The cold-start binding must reference the cold-start ClusterRole."
  }
  assert {
    condition     = length(kubernetes_cluster_role_binding_v1.cold_start_read.subject) == 1 && kubernetes_cluster_role_binding_v1.cold_start_read.subject[0].kind == "ServiceAccount" && kubernetes_cluster_role_binding_v1.cold_start_read.subject[0].name == "prefect-worker" && kubernetes_cluster_role_binding_v1.cold_start_read.subject[0].namespace == "prefect"
    error_message = "The cold-start ClusterRole must be bound to the worker's ServiceAccount alone."
  }

  # --- Namespaced: list events where vllm-gpu runs ---

  # Kubelet's Pulling/Pulled and the scheduler's events are core v1 events in the
  # slipstream namespace; list is the only verb, so the worker cannot create or delete them.
  assert {
    condition     = length([for r in kubernetes_role_v1.vllm_gpu_manage.rule : r if contains(r.resources, "events")]) == 1 && length([for r in kubernetes_role_v1.vllm_gpu_manage.rule : r if toset(r.api_groups) == toset([""]) && toset(r.resources) == toset(["events"]) && toset(r.verbs) == toset(["list"])]) == 1
    error_message = "The worker's slipstream Role must grant list on core events and no other verb on them."
  }
}
