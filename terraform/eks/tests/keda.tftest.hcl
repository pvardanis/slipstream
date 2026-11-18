# Plan-level tests for the KEDA add-on on the eks stack (ADR-0031, #567): the Helm
# release that installs KEDA's operator, metrics server, admission webhooks and CRDs,
# ready for the serving fleet's ScaledObject. They run offline — the aws, helm, and
# kubernetes providers are mocked — so the assertions check the install's shape (the
# release, its pinned chart and namespace, wait/atomic) without standing a cluster up.
# KEDA serving on a live cluster is the manual tier, checked after cluster-up: the
# scaledobjects.keda.sh CRD exists, the v1beta1.external.metrics.k8s.io APIService is
# Available, and a ScaledObject passes a server-side dry-run apply through the webhook.

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

run "keda_release_shape" {
  command = plan

  # No other release creates the keda namespace, so this one does.
  assert {
    condition     = helm_release.keda.name == "keda" && helm_release.keda.namespace == "keda" && helm_release.keda.create_namespace
    error_message = "KEDA must install as the keda release in the keda namespace, which the release creates."
  }
  assert {
    condition     = helm_release.keda.repository == "https://kedacore.github.io/charts" && helm_release.keda.chart == "keda"
    error_message = "KEDA must come from the project's chart repository, kedacore/charts."
  }

  # The ScaledObject CRD is what the serving fleet's app needs from this release.
  assert {
    condition     = yamldecode(helm_release.keda.values[0]).crds.install
    error_message = "The KEDA release must install the chart's CRDs, ScaledObject among them."
  }

  # KEDA's compatibility matrix (keda.sh/docs/2.21/operate/cluster/, read 2026-11-18)
  # tests v2.21 against Kubernetes v1.34–v1.36, which covers the cluster's 1.36. The two
  # asserts below fail if either the chart pin or the cluster version moves away from the
  # pair that was checked.
  assert {
    condition     = helm_release.keda.version == "2.21.1"
    error_message = "The KEDA chart must be pinned to 2.21.1, the version checked against KEDA's compatibility matrix for EKS 1.36. Check a new version against the matrix before moving this pin."
  }
  assert {
    condition     = var.kubernetes_version == "1.36"
    error_message = "The cluster's Kubernetes version moved off 1.36: recheck the KEDA chart pin against KEDA's compatibility matrix."
  }

  # A broken install (operator, metrics server or webhook never Ready) must fail
  # cluster-up loudly and roll back, not leave a ScaledObject to be rejected by a webhook
  # that never came up.
  assert {
    condition     = helm_release.keda.wait && helm_release.keda.atomic
    error_message = "The KEDA release must set wait and atomic."
  }
}
