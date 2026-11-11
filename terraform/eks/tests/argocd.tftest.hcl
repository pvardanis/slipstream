# Plan-level tests for the Argo CD bootstrap on the eks stack (#509): the pinned Argo CD
# install, and the argocd-apps release that renders the two AppProjects and the root
# app-of-apps Application — the single handoff from Terraform to Argo CD. They run
# offline (aws, helm and kubernetes mocked), so they assert the install's shape: the
# chart pins, the root Application's cluster values and sync policy, and each project's
# scope. The root Application actually reporting Synced and Healthy is the cloud tier.
#
# The eks module builds IAM roles whose assume_role_policy is validated as JSON at
# plan; the mock provider's random string is not valid JSON, so every policy
# document gets a valid (empty) JSON default here. Mirrors karpenter.tftest.hcl.
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

# The Karpenter chart is pulled through a second, us-east-1-pinned aws provider
# (public ECR's token API); mocking the default aws does not cover a distinct
# provider configuration, so without this the test makes a real STS call.
mock_provider "aws" {
  alias = "virginia"
}
mock_provider "helm" {}
mock_provider "kubernetes" {}

# The worker layer plans with the stack and reads the bootstrap remote state; stub it
# instead of reaching for real S3.
override_data {
  target = data.terraform_remote_state.bootstrap
  values = {
    outputs = {
      orchestration_image_repo_url    = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
      bench_image_repo_arn            = "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream-bench"
      bench_results_bucket_arn        = "arn:aws:s3:::slipstream-bench-results-abc123"
      sweep_dispatch_role_arn         = "arn:aws:iam::111122223333:role/slipstream-sweep-dispatch"
      grafana_cloud_write_secret_name = "grafana-cloud/write"
      cluster_name                    = "slipstream"
      prefect_namespace               = "prefect"
    }
  }
}

# The metrics stack (monitoring.tf) reads the grafana stack's remote_write outputs; stub
# them instead of reaching for real S3.
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

run "argocd_install_is_pinned" {
  command = plan

  # The literal pins are the source of truth: bumping a chart is a deliberate change
  # that has to touch this test too, so an apply can never float onto an untested chart.
  assert {
    condition     = helm_release.argocd.repository == "https://argoproj.github.io/argo-helm"
    error_message = "Argo CD must install from the upstream argo-helm repository."
  }
  assert {
    condition     = helm_release.argocd.chart == "argo-cd"
    error_message = "The Argo CD release must deploy the argo-cd chart."
  }
  assert {
    condition     = helm_release.argocd.version == "10.17.2"
    error_message = "The argo-cd chart must be pinned to 10.17.2 (Argo CD v3.6.1)."
  }
  assert {
    condition     = helm_release.argocd.namespace == "argocd"
    error_message = "Argo CD must install into the argocd namespace."
  }

  # A broken install (crashlooping controller, bad CRDs) fails cluster-up loudly and
  # rolls back, rather than leaving a half-up Argo CD the root Application lands on.
  assert {
    condition     = helm_release.argocd.wait == true && helm_release.argocd.atomic == true
    error_message = "The Argo CD release must wait and be atomic so a broken install fails the apply."
  }

  # No SSO is configured (the UI is reached by port-forward with the admin login), so
  # Dex would be an idle pod on the CPU node.
  assert {
    condition     = yamldecode(helm_release.argocd.values[0]).dex.enabled == false
    error_message = "Dex must be disabled: no SSO is configured."
  }
}
