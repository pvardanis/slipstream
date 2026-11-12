# Plan-level tests for Argo CD Image Updater on the eks stack (#512), the single writer of
# the Prefect worker's image tag: the pinned install, its ECR registry and login script,
# the ImageUpdater resource that reads the annotated Applications, and its read-only ECR
# access through Pod Identity. They run offline (aws, helm and kubernetes mocked), so they
# assert the install's shape; Image Updater actually rolling the worker is the cloud tier.
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

run "image_updater_install_is_pinned" {
  command = plan

  # The test repeats the pin as a literal, so bumping the chart default in variables.tf is
  # a deliberate change that has to touch this test too.
  assert {
    condition     = helm_release.image_updater.repository == "https://argoproj.github.io/argo-helm"
    error_message = "Image Updater must install from the upstream argo-helm repository."
  }
  assert {
    condition     = helm_release.image_updater.chart == "argocd-image-updater"
    error_message = "The Image Updater release must deploy the argocd-image-updater chart."
  }
  assert {
    condition     = helm_release.image_updater.version == "1.4.2"
    error_message = "The argocd-image-updater chart must be pinned to 1.4.2."
  }
  assert {
    condition     = helm_release.image_updater.namespace == "argocd"
    error_message = "Image Updater must install into Argo CD's namespace, where the Applications it updates live."
  }
  assert {
    condition     = helm_release.image_updater.wait == true && helm_release.image_updater.atomic == true
    error_message = "The Image Updater release must wait and be atomic so a broken install fails the apply."
  }

  # The orchestration repository's registry, logged in to by the mounted script, whose
  # credentials are re-read before ECR's 12 h token expires.
  assert {
    condition = yamldecode(helm_release.image_updater.values[0]).config.registries == [{
      name        = "ECR"
      prefix      = "111122223333.dkr.ecr.eu-west-1.amazonaws.com"
      api_url     = "https://111122223333.dkr.ecr.eu-west-1.amazonaws.com"
      credentials = "ext:/scripts/ecr-login.sh"
      credsexpire = "11h"
    }]
    error_message = "Image Updater must log in to the orchestration image's ECR registry with the mounted script, refreshing its credentials before the 12 h token expires."
  }
  assert {
    condition     = yamldecode(helm_release.image_updater.values[0]).authScripts.enabled == true
    error_message = "Image Updater must mount the ECR login script under /scripts."
  }
  assert {
    condition     = strcontains(yamldecode(helm_release.image_updater.values[0]).authScripts.scripts["ecr-login.sh"], "aws ecr get-login-password")
    error_message = "The ECR login script must fetch a token with aws ecr get-login-password."
  }
  assert {
    condition     = contains(yamldecode(helm_release.image_updater.values[0]).extraEnv, { name = "AWS_REGION", value = "eu-west-1" })
    error_message = "The login script must reach ECR in the stack's region."
  }
  assert {
    condition     = yamldecode(helm_release.image_updater.values[0]).serviceAccount == { create = true, name = "argocd-image-updater" }
    error_message = "The Image Updater chart must create the argocd-image-updater ServiceAccount Pod Identity binds."
  }
}

# Image Updater reads its per-app settings from the annotations on the Applications that
# carry the selector label, so the worker's settings live beside its Application in git.
run "image_updater_reads_annotated_applications" {
  command = plan

  assert {
    condition     = helm_release.image_updater_apps.chart == "${path.module}/charts/image-updater-apps"
    error_message = "The ImageUpdater resource must come from the local image-updater-apps chart."
  }
  assert {
    condition     = helm_release.image_updater_apps.namespace == "argocd"
    error_message = "The ImageUpdater resource must live in Argo CD's namespace, where it selects Applications."
  }
  assert {
    condition     = helm_release.image_updater_apps.wait == true && helm_release.image_updater_apps.atomic == true
    error_message = "The ImageUpdater release must wait and be atomic so a rejected resource fails the apply."
  }
}

run "image_updater_ecr_access_is_read_only" {
  command = plan

  assert {
    condition     = jsondecode(aws_iam_role.image_updater.assume_role_policy).Statement[0].Principal.Service == "pods.eks.amazonaws.com"
    error_message = "The Image Updater role must trust the EKS Pod Identity service principal."
  }
  assert {
    condition     = toset(jsondecode(aws_iam_role.image_updater.assume_role_policy).Statement[0].Action) == toset(["sts:AssumeRole", "sts:TagSession"])
    error_message = "The Image Updater trust policy must allow sts:AssumeRole and sts:TagSession (the Pod Identity agent tags the session)."
  }
  assert {
    condition     = aws_eks_pod_identity_association.image_updater.namespace == "argocd"
    error_message = "The Pod Identity association must target the argocd namespace Image Updater runs in."
  }
  assert {
    condition     = aws_eks_pod_identity_association.image_updater.service_account == "argocd-image-updater"
    error_message = "The Pod Identity association must bind the argocd-image-updater ServiceAccount."
  }

  # The registry token is account-wide (GetAuthorizationToken takes no resource); every
  # other right reads the orchestration repository alone: list its tags, read its
  # manifests and the config blob newest-build orders by. Nothing pushes or deletes.
  assert {
    condition = jsondecode(aws_iam_role_policy.image_updater.policy).Statement == [
      {
        Sid      = "EcrAuthorizationToken"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid      = "ReadOrchestrationImages"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:ListImages"]
        Resource = "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream/orchestration"
      },
    ]
    error_message = "Image Updater's ECR access must be the registry token plus read-only rights on the orchestration repository."
  }
}
