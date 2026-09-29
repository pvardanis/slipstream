# Dynamic block storage for the cluster: the EBS CSI driver's IAM role and the
# default gp3 StorageClass its addon (main.tf) provisions against. The cluster ships
# no default StorageClass, so an unqualified PVC — the one the Prefect server's chart
# creates for its SQLite state (#185) — has nothing to bind without this.

# Authenticates the Kubernetes provider to the cluster with the caller's AWS identity
# via `aws eks get-token`, the same credentials the Helm provider and kubectl use.
provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
  }
}

# The EBS CSI controller assumes this role through the Pod Identity association wired
# in main.tf; Pod Identity's trust is the eks-pod-identity-agent service principal,
# with sts:TagSession alongside AssumeRole (the agent tags the session).
data "aws_iam_policy_document" "ebs_csi_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${var.cluster_name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_assume.json
  tags               = local.tags
}

# The AWS-managed policy scoped to exactly what the driver needs to create, attach,
# and delete EBS volumes for PVCs.
resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# The one dynamic provisioner on this cluster, and its default: an unqualified PVC
# binds here. WaitForFirstConsumer defers provisioning until the consuming pod is
# scheduled, so the volume lands in that pod's AZ (immediate binding can strand a
# zonal EBS volume in another AZ and leave the pod Pending). Reclaim Delete makes the
# volume die with its PVC, so cluster-down's Helm uninstall reaps the EBS volume and
# nothing outlives the cluster. Provisioned volumes carry the Project tag the
# zero-leak sweep filters on, so an orphan is caught rather than silently billed
# (ADR-0002 money-safety).
resource "kubernetes_storage_class_v1" "gp3" {
  metadata {
    name = "gp3"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  volume_binding_mode    = "WaitForFirstConsumer"
  reclaim_policy         = "Delete"
  allow_volume_expansion = true

  parameters = {
    type               = "gp3"
    tagSpecification_1 = "Project=slipstream"
    tagSpecification_2 = "ManagedBy=ebs-csi"
  }
}
