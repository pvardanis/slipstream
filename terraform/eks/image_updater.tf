# Argo CD Image Updater on the eks stack (#512): the single writer of the Prefect worker's
# image tag, so a merge to main is what the worker runs without re-applying this stack.
# It polls the orchestration ECR repository and, for each Application that opts in
# through annotations (k8s/argocd/apps/templates/prefect-worker.yaml), writes the newest
# matching tag onto that Application as a Helm parameter (the `argocd` write-back; git
# write-back is ruled out because main's required check blocks bot pushes). Like Argo CD
# it rides the cluster's duty-cycle — installed by `cluster-up`, removed by `cluster-down`.
#
# Auth is keyless: a Pod Identity association grants its ServiceAccount the ECR registry
# token and read-only rights on the orchestration repository alone.

locals {
  image_updater_service_account = "argocd-image-updater"

  # The registry host the orchestration repository lives in, which Image Updater matches
  # image names against.
  orchestration_registry = split("/", local.orchestration_repo_url)[0]

  # Helm values as a map so the test can read the wiring back. The login script runs in
  # the Image Updater container (which ships the aws CLI) under its Pod Identity
  # credentials and prints the `AWS:<token>` pair the registry accepts. ECR tokens last
  # 12 h, so the credentials are re-read after 11 h, before they expire. The password is
  # assigned before it is printed: a failing substitution inside an echo argument does not
  # trip `set -e`, so the assignment is what makes a failed login fail the script.
  image_updater_helm_values = {
    serviceAccount = { create = true, name = local.image_updater_service_account }
    extraEnv       = [{ name = "AWS_REGION", value = var.region }]
    authScripts = {
      enabled = true
      scripts = {
        "ecr-login.sh" = <<-EOT
          #!/bin/sh
          set -eu
          password="$(aws ecr get-login-password --region "$${AWS_REGION}")"
          echo "AWS:$${password}"
        EOT
      }
    }
    config = {
      registries = [{
        name        = "ECR"
        prefix      = local.orchestration_registry
        api_url     = "https://${local.orchestration_registry}"
        credentials = "ext:/scripts/ecr-login.sh"
        credsexpire = "11h"
      }]
    }
  }
}

resource "helm_release" "image_updater" {
  namespace        = local.argocd_namespace
  create_namespace = false
  name             = "argocd-image-updater"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argocd-image-updater"
  version          = var.argocd_image_updater_chart_version

  # Block apply until Image Updater is Ready and roll back atomically on failure, so a
  # broken install fails cluster-up loudly rather than leaving the worker's tag unwritten.
  wait   = true
  atomic = true

  values = [yamlencode(local.image_updater_helm_values)]

  # Installs into Argo CD's namespace once Argo CD and its Application CRD exist. Pod
  # Identity credentials are injected when a pod is admitted, so the association exists
  # before the Image Updater pod starts.
  depends_on = [
    helm_release.argocd,
    aws_eks_pod_identity_association.image_updater,
  ]
}

# The ImageUpdater resource that points Image Updater at the Applications carrying its
# selector label and has it read their annotations. A chart of its own, installed after
# the Image Updater release, because its kind exists only once that release's CRD does.
resource "helm_release" "image_updater_apps" {
  namespace = local.argocd_namespace
  name      = "image-updater-apps"
  chart     = "${path.module}/charts/image-updater-apps"

  wait   = true
  atomic = true

  depends_on = [helm_release.image_updater]
}

# Image Updater assumes this role through the Pod Identity association below, with the
# same trust as the worker's role (worker.tf). jsonencode inline so the plan test can read
# the statements back.
resource "aws_iam_role" "image_updater" {
  name = "${var.cluster_name}-argocd-image-updater"

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

# GetAuthorizationToken takes no resource, so the registry token is account-wide; every
# other right reads the orchestration repository alone: its tag list, its manifests, and
# the config blob newest-build orders tags by. Nothing pushes or deletes.
resource "aws_iam_role_policy" "image_updater" {
  name = "${var.cluster_name}-argocd-image-updater"
  role = aws_iam_role.image_updater.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrAuthorizationToken"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid      = "ReadOrchestrationImages"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer", "ecr:ListImages"]
        Resource = data.terraform_remote_state.bootstrap.outputs.orchestration_image_repo_arn
      },
    ]
  })
}

resource "aws_eks_pod_identity_association" "image_updater" {
  cluster_name    = module.eks.cluster_name
  namespace       = local.argocd_namespace
  service_account = local.image_updater_service_account
  role_arn        = aws_iam_role.image_updater.arn
  tags            = local.tags
}
