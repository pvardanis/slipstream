# A minimal EKS cluster: a VPC and one small on-demand CPU node group,
# reachable via kubectl. Credentials come from the caller's AWS_PROFILE;
# this stack never handles them.
provider "aws" {
  region = var.region
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # Three AZs is the EKS minimum for a resilient control plane.
  azs = slice(data.aws_availability_zones.available.names, 0, 3)

  tags = {
    Project   = "slipstream"
    ManagedBy = "terraform"
  }
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${var.cluster_name}-vpc"
  cidr = var.vpc_cidr

  azs             = local.azs
  private_subnets = [for k in range(3) : cidrsubnet(var.vpc_cidr, 4, k)]
  public_subnets  = [for k in range(3) : cidrsubnet(var.vpc_cidr, 8, k + 48)]

  # A single NAT gateway keeps idle spend down; nodes still get egress.
  enable_nat_gateway = true
  single_nat_gateway = true

  # Tags the EKS control plane and load balancers look for during subnet discovery.
  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }

  tags = local.tags
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version

  # Access Entries model; grant the caller running `just up` cluster admin.
  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = true

  endpoint_public_access = true

  addons = {
    coredns                = {}
    eks-pod-identity-agent = { before_compute = true }
    kube-proxy             = {}
    vpc-cni                = { before_compute = true }
  }

  vpc_id                   = module.vpc.vpc_id
  subnet_ids               = module.vpc.private_subnets
  control_plane_subnet_ids = module.vpc.private_subnets

  eks_managed_node_groups = {
    cpu = {
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = [var.node_instance_type]
      capacity_type  = "ON_DEMAND"

      min_size     = 1
      max_size     = 1
      desired_size = 1
    }
  }

  tags = local.tags
}
