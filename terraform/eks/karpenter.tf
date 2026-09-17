# Karpenter control plane: the eks module's Karpenter submodule (controller IAM
# role + Pod Identity association, node IAM role, SQS spot-interruption queue and
# its EventBridge rules) plus the controller Helm release wired to them. The GPU
# NodePool and EC2NodeClass are raw manifests applied separately (#90); this file
# is only the install (ADR-0006).

# Public ECR (where the Karpenter chart lives) issues auth tokens only from
# us-east-1, so the token is read through a second, region-pinned aws provider.
provider "aws" {
  alias  = "virginia"
  region = "us-east-1"
}

data "aws_ecrpublic_authorization_token" "token" {
  provider = aws.virginia
}

# Authenticates the Helm provider to the cluster with the caller's AWS identity
# via `aws eks get-token`, the same credentials `just up` uses for kubectl.
provider "helm" {
  kubernetes = {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
    }
  }
}

module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "~> 21.0"

  cluster_name = module.eks.cluster_name

  # The cluster runs the eks-pod-identity-agent addon, so the controller
  # authenticates via a Pod Identity association (the module default), no OIDC
  # provider or IRSA. The module also stands up the node IAM role and the SQS
  # interruption queue by default; both are left on deliberately.
  node_iam_role_additional_policies = {
    AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  }

  tags = local.tags
}

locals {
  # Helm values as a map so the test can read the wiring back; encoded to YAML for
  # the release below. serviceAccount.name must match the Pod Identity association
  # the module created, or the controller has no AWS permissions.
  karpenter_helm_settings = {
    clusterName       = module.eks.cluster_name
    clusterEndpoint   = module.eks.cluster_endpoint
    interruptionQueue = module.karpenter.queue_name
  }
}

resource "helm_release" "karpenter" {
  namespace  = "kube-system"
  name       = "karpenter"
  repository = "oci://public.ecr.aws/karpenter"
  # Public-ECR pull auth for the chart itself.
  repository_username = data.aws_ecrpublic_authorization_token.token.user_name
  repository_password = data.aws_ecrpublic_authorization_token.token.password
  chart               = "karpenter"
  version             = var.karpenter_chart_version

  # The CRDs the release installs settle asynchronously; not waiting keeps apply
  # from blocking on a controller that has no NodePool to act on yet (#90).
  wait = false

  values = [
    yamlencode({
      serviceAccount = { name = module.karpenter.service_account }
      settings       = local.karpenter_helm_settings
    })
  ]
}
