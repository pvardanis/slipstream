# The External Secrets Operator, which brings the Grafana Cloud write token from Secrets
# Manager into the cluster without it passing through Terraform state (ADR-0026,
# "Credentials"). Like Karpenter and Prefect it rides the cluster's duty-cycle: up with
# `cluster-up`, gone with `cluster-down`.

locals {
  external_secrets_namespace       = "external-secrets"
  external_secrets_service_account = "external-secrets"

  # The write token's secret, named by the bootstrap stack that creates it. try() turns a
  # bootstrap applied before the output existed into the policy's actionable precondition
  # failure below. Secrets Manager appends six random characters to a secret's ARN, so
  # -?????? matches exactly this secret and not one whose name merely starts with it.
  grafana_cloud_write_secret_name = try(data.terraform_remote_state.bootstrap.outputs.grafana_cloud_write_secret_name, "")
  grafana_cloud_write_secret_arn  = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${local.grafana_cloud_write_secret_name}-??????"

  # Helm values as a map so the test can read the wiring back; encoded to YAML for the
  # release below. serviceAccount.name must match the Pod Identity association, or the
  # controller has no AWS credentials.
  external_secrets_helm_values = {
    serviceAccount = { name = local.external_secrets_service_account }
  }
}

# The controller assumes this role through the Pod Identity association below; the trust
# is the pods.eks.amazonaws.com principal with sts:TagSession alongside AssumeRole (the
# agent tags the session). jsonencode inline so the plan test can read it back, as in
# worker.tf.
resource "aws_iam_role" "external_secrets" {
  name = "${var.cluster_name}-external-secrets"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })

  tags = local.tags
}

# Read on the write token only. Syncing a secret's current value is a single
# GetSecretValue call; DescribeSecret and the rest are for pushing secrets, which this
# cluster never does. The secret uses the AWS-managed key, whose key policy allows
# decryption through Secrets Manager, so no KMS grant is needed.
resource "aws_iam_role_policy" "external_secrets" {
  name = "${var.cluster_name}-external-secrets"
  role = aws_iam_role.external_secrets.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadGrafanaCloudWriteToken"
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = local.grafana_cloud_write_secret_arn
    }]
  })

  lifecycle {
    precondition {
      condition     = local.grafana_cloud_write_secret_name != ""
      error_message = "The bootstrap stack has no grafana_cloud_write_secret_name output, so there is no Grafana Cloud write token to sync. Run `just bootstrap`, then `just grafana-cloud-setup` to fill the secret."
    }
  }
}

# Binds the chart's ServiceAccount to the role. Standalone, as in worker.tf, so the plan
# test can assert its namespace/SA wiring.
resource "aws_eks_pod_identity_association" "external_secrets" {
  cluster_name    = module.eks.cluster_name
  namespace       = local.external_secrets_namespace
  service_account = local.external_secrets_service_account
  role_arn        = aws_iam_role.external_secrets.arn
  tags            = local.tags
}

resource "helm_release" "external_secrets" {
  namespace        = local.external_secrets_namespace
  create_namespace = true
  name             = "external-secrets"
  repository       = "https://charts.external-secrets.io"
  chart            = "external-secrets"
  version          = var.external_secrets_chart_version

  # Block apply until the controller, webhook and cert-controller are Ready and roll back
  # atomically on failure, so a broken install fails the apply loudly rather than leaving
  # the sync release below to be rejected by a webhook that never came up.
  wait   = true
  atomic = true

  values = [yamlencode(local.external_secrets_helm_values)]

  # Pod Identity credentials are injected when a pod is admitted, so the association must
  # exist before the controller pod starts; the role's policy too, or the first sync is
  # denied. module.eks brings the node group and the Pod Identity agent the pods need.
  depends_on = [module.eks, aws_eks_pod_identity_association.external_secrets, aws_iam_role_policy.external_secrets]
}

# The namespace Prometheus runs in, created here so the write token has somewhere to land
# at cluster-up; the metrics release installs into it.
resource "kubernetes_namespace_v1" "monitoring" {
  metadata {
    name = "monitoring"
  }
}

# The namespaces that send to Grafana Cloud, each needing its own copy of the write token:
# monitoring for Prometheus remote_write, slipstream (worker.tf) for the Collector's OTLP
# exporter. Static keys so for_each is known at plan.
locals {
  grafana_cloud_write_token_namespaces = {
    monitoring = kubernetes_namespace_v1.monitoring.metadata[0].name
    slipstream = kubernetes_namespace_v1.slipstream.metadata[0].name
  }
}

# A SecretStore and an ExternalSecret in each of those namespaces. Releases of their own
# because their kinds exist only once the operator's CRDs do, and Helm resolves every kind
# in a release before installing any of it. The token itself never enters Terraform state;
# only the reference to it does.
resource "helm_release" "grafana_cloud_write_token" {
  for_each = local.grafana_cloud_write_token_namespaces

  namespace = each.value
  name      = "grafana-cloud-write-token"
  chart     = "${path.module}/charts/grafana-cloud-write-token"

  # The operator's webhook validates the store and ExternalSecret as they are created, so
  # a rejected one fails the apply and rolls back. Helm does not wait on an ExternalSecret's
  # sync, so an empty or unreadable secret leaves the apply green and shows only in the
  # ExternalSecret's status.
  wait   = true
  atomic = true

  values = [yamlencode({
    region    = var.region
    remoteKey = local.grafana_cloud_write_secret_name
  })]

  # On destroy these uninstall first, so the ExternalSecrets and the Secrets they own are
  # deleted while the operator that handles them is still running.
  depends_on = [helm_release.external_secrets]
}
