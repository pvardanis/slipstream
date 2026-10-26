# Plan-level tests for the External Secrets Operator layer on the eks stack (ADR-0026,
# #366): the operator's Helm release, its Pod Identity role and policy, and the release
# that syncs the Grafana Cloud write token into the monitoring and slipstream namespaces.
# They run offline — the aws, helm, and kubernetes providers are mocked — so the
# assertions check the install's shape (pinned chart, wait/atomic, a policy that reads one
# secret and nothing else, both target namespaces) without standing a cluster up. The
# token actually landing in both namespaces and both ExternalSecrets reporting synced is
# the manual tier: `just cluster-up`, then
# `kubectl get externalsecret -n monitoring` and `-n slipstream`.

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

variables {
  state_bucket = "slipstream-tfstate-abc123"
}

run "external_secrets_operator_shape" {
  command = plan

  # An exact x.y.z, never a range or an empty string Helm would resolve to the latest
  # chart: a cluster-up then installs the same operator every session.
  assert {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", helm_release.external_secrets.version))
    error_message = "The External Secrets Operator chart must be pinned to an exact x.y.z version."
  }
  assert {
    condition     = helm_release.external_secrets.repository == "https://charts.external-secrets.io"
    error_message = "The External Secrets Operator chart must come from the project's chart repository."
  }

  # A broken install (bad image, webhook never Ready) must fail cluster-up loudly and
  # roll back, not leave a half-up operator the sync release then cannot reach.
  assert {
    condition     = helm_release.external_secrets.wait && helm_release.external_secrets.atomic
    error_message = "The External Secrets Operator release must set wait and atomic."
  }
}

run "external_secrets_reads_only_the_write_token" {
  command = plan

  # One statement, one action, one secret: the operator can read the write token and
  # nothing else in Secrets Manager — not grafana-cloud/api, not any other secret. The
  # trailing -?????? matches the six random characters Secrets Manager appends to the ARN.
  assert {
    condition = jsondecode(aws_iam_role_policy.external_secrets.policy).Statement == [{
      Sid      = "ReadGrafanaCloudWriteToken"
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = "arn:aws:secretsmanager:eu-west-1:111122223333:secret:grafana-cloud/write-??????"
    }]
    error_message = "The operator's policy must grant only secretsmanager:GetSecretValue on grafana-cloud/write."
  }

  # Pod Identity: the trust is the pods.eks.amazonaws.com principal, with sts:TagSession
  # alongside AssumeRole (the agent tags the session).
  assert {
    condition = jsondecode(aws_iam_role.external_secrets.assume_role_policy).Statement == [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
    error_message = "The operator's role must trust only EKS Pod Identity."
  }

  # The association must name the ServiceAccount the chart creates, in the release's
  # namespace, or the controller runs with no AWS credentials and every sync fails.
  assert {
    condition     = aws_eks_pod_identity_association.external_secrets.namespace == helm_release.external_secrets.namespace
    error_message = "The Pod Identity association must be in the operator release's namespace."
  }
  assert {
    condition     = aws_eks_pod_identity_association.external_secrets.service_account == yamldecode(helm_release.external_secrets.values[0]).serviceAccount.name
    error_message = "The Pod Identity association must bind the ServiceAccount the chart creates."
  }
}

run "write_token_synced_into_both_namespaces" {
  command = plan

  # A Secret is read only from its own namespace: Prometheus remote_write reads it in
  # monitoring, the Collector's OTLP exporter in slipstream. One release per namespace,
  # each installing its store and ExternalSecret there.
  assert {
    condition     = sort([for r in helm_release.grafana_cloud_write_token : r.namespace]) == tolist(["monitoring", "slipstream"])
    error_message = "The write token must sync into the monitoring and slipstream namespaces."
  }
  # Helm ignores a values key the chart does not read, so a key renamed on one side only
  # leaves the chart's empty default, which the templates' required rejects at install,
  # not here.
  assert {
    condition     = alltrue([for r in helm_release.grafana_cloud_write_token : sort(keys(yamldecode(r.values[0]))) == sort(keys(yamldecode(file("charts/grafana-cloud-write-token/values.yaml"))))])
    error_message = "Every sync release must set exactly the keys the chart's values.yaml declares."
  }
  assert {
    condition     = alltrue([for r in helm_release.grafana_cloud_write_token : yamldecode(r.values[0]).remoteKey == "grafana-cloud/write"])
    error_message = "Every sync must read the grafana-cloud/write secret the operator's policy grants."
  }
  assert {
    condition     = alltrue([for r in helm_release.grafana_cloud_write_token : yamldecode(r.values[0]).region == "eu-west-1"])
    error_message = "Every secret store must read Secrets Manager in the stack's region."
  }

  # No stack creates monitoring before this one, so the eks stack owns it.
  assert {
    condition     = kubernetes_namespace_v1.monitoring.metadata[0].name == "monitoring"
    error_message = "The eks stack must create the monitoring namespace the token syncs into."
  }

  # A store or ExternalSecret the operator's webhook rejects must fail cluster-up loudly.
  assert {
    condition     = alltrue([for r in helm_release.grafana_cloud_write_token : r.wait && r.atomic])
    error_message = "Every write-token sync release must set wait and atomic."
  }
}

# A bootstrap applied before it published the write token's secret has no
# grafana_cloud_write_secret_name output; the policy's precondition fails with an
# actionable message rather than an unsupported-attribute error.
run "external_secrets_rejects_a_bootstrap_without_the_write_secret" {
  command = plan

  override_data {
    target = data.terraform_remote_state.bootstrap
    values = {
      outputs = {
        orchestration_image_repo_url = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
        sweep_dispatch_role_arn      = "arn:aws:iam::111122223333:role/slipstream-sweep-dispatch"
        cluster_name                 = "slipstream"
        prefect_namespace            = "prefect"
      }
    }
  }

  expect_failures = [aws_iam_role_policy.external_secrets]
}
