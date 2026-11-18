# Plan-level tests for the serving namespace on the eks stack (ADR-0031, #569): the
# namespace the serving fleet's Argo CD app syncs into, and the vllm-api-key Secret the
# fleet's pods read there. They run offline — the aws, helm, and kubernetes providers are
# mocked — so the assertions check the resources' shape. The key is write-only, so its
# value is never in the plan or the state, and no test reads it.

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

run "serving_namespace_holds_the_api_key_secret" {
  command = plan

  # Terraform creates the namespace, not Argo CD: the Secret has to land before the
  # serving fleet's app syncs, and Argo CD does not delete a namespace it did not create,
  # so pruning the app cannot take the Secret with it.
  assert {
    condition     = kubernetes_namespace_v1.serving.metadata[0].name == "serving"
    error_message = "The eks stack must create the serving namespace."
  }

  # The name and key the serving fleet's pods read (k8s/serving/vllm-serving.yaml).
  assert {
    condition     = kubernetes_secret_v1.serving_api_key.metadata[0].name == "vllm-api-key" && kubernetes_secret_v1.serving_api_key.metadata[0].namespace == "serving"
    error_message = "The api-key Secret must be vllm-api-key in the serving namespace."
  }
  assert {
    condition     = strcontains(file("${path.module}/../../k8s/serving/vllm-serving.yaml"), "name: vllm-api-key\n              key: api-key")
    error_message = "The serving fleet's manifest must read the api-key from the vllm-api-key Secret's api-key key."
  }

  # The key goes through the write-only attribute, so it is in neither plan nor state;
  # Terraform writes it only when the revision changes, so applies keep the key stable.
  assert {
    condition     = kubernetes_secret_v1.serving_api_key.data_wo_revision == 1
    error_message = "The api-key must be written through data_wo at revision 1."
  }
}
