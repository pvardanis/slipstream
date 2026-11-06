# The Prefect worker's rights to apply and destroy the bench-endpoint stack, so a flow run
# can stand its own bench endpoint up and tear it down (#470). Two grants, each as narrow
# as the stack allows — an AWS policy scoped by the stack's name prefixes and Component tag,
# and a get on the one vLLM api-key Secret the stack publishes — plus the cluster topology
# the stack is applied with, in the worker's environment.
#
# The bench-endpoint stack creates no IAM (bench_host.tf owns the host's role and profile),
# so the worker needs iam:PassRole on that one role and no IAM create, attach, or put: its
# infra rights cannot be turned into admin.

locals {
  # The bench-endpoint stack tags its resources with local.tags, Component=bench-endpoint
  # (terraform/bench-endpoint/data.tf), and the EC2 and ACM grants below are conditioned on
  # that tag. The load balancer, target group, listener, trust store, client secret, and
  # trust-store bucket are granted by name prefix instead: local.bench_endpoint_name
  # (bench_host.tf) for the secret and bucket, and "bench-" for the load balancer, target
  # group, and trust store, whose 6-character name_prefix limit leaves no room for the
  # longer name. The listener is untagged and its ARN carries its load balancer's name.
  bench_endpoint_component_tag  = "bench-endpoint"
  bench_endpoint_trust_store    = "arn:aws:s3:::${local.bench_endpoint_name}-truststore-*"
  bench_endpoint_ec2_arn_prefix = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}"
  bench_endpoint_elb_arn_prefix = "arn:aws:elasticloadbalancing:${var.region}:${data.aws_caller_identity.current.account_id}"
  bench_endpoint_state_key      = "bench-endpoint/terraform.tfstate"

  # The node autoscaling groups, shared by the node_autoscaling_groups output (the manual
  # bench-endpoint-up path) and the worker's environment, so both apply the same value.
  node_autoscaling_group_names = flatten([for ng in module.eks.eks_managed_node_groups : ng.node_group_autoscaling_group_names])

  # The worker's environment: what the stack is applied with. The state bucket backs the
  # stack's init; the topology and instance profile are its input variables, read here
  # rather than from the eks state so the worker needs no read on it. The lists are JSON,
  # the form Terraform takes a list variable in.
  bench_endpoint_topology = {
    BENCH_ENDPOINT_STATE_BUCKET            = var.state_bucket
    BENCH_ENDPOINT_VPC_ID                  = module.vpc.vpc_id
    BENCH_ENDPOINT_PUBLIC_SUBNETS          = jsonencode(module.vpc.public_subnets)
    BENCH_ENDPOINT_NODE_SECURITY_GROUP_ID  = module.eks.node_security_group_id
    BENCH_ENDPOINT_NODE_AUTOSCALING_GROUPS = jsonencode(local.node_autoscaling_group_names)
    BENCH_ENDPOINT_INSTANCE_PROFILE        = aws_iam_instance_profile.bench_host.name
  }
}

# A separate inline policy from the worker's S3/SSM one (worker.tf), so the two grants
# read one at a time. Built only from values known at plan — names, tags, the account id,
# never a module's computed id — so the plan test can read every statement back.
resource "aws_iam_role_policy" "prefect_worker_bench_endpoint" {
  name = "${var.cluster_name}-prefect-worker-bench-endpoint"
  role = aws_iam_role.prefect_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Launch the bench host under the eks-owned profile, toward EC2 only.
      {
        Sid       = "BenchHostPassRole"
        Effect    = "Allow"
        Action    = ["iam:PassRole"]
        Resource  = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${aws_iam_role.bench_host.name}"
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
      # Terraform reads every resource back on refresh and after create. These read
      # actions take no resource scope.
      {
        Sid    = "BenchEndpointRead"
        Effect = "Allow"
        Action = [
          "ec2:Describe*",
          "elasticloadbalancing:Describe*",
          "autoscaling:Describe*",
        ]
        Resource = "*"
      },
      # The bench host's AMI, resolved from the public Amazon Linux 2023 parameter.
      {
        Sid      = "BenchEndpointAmiParameter"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.region}::parameter/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
      },
      # These two statements cover the stack's own state and its native lockfile only.
      # Taking the lock writes the lockfile and releasing it deletes it.
      {
        Sid      = "BenchEndpointState"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "arn:aws:s3:::${var.state_bucket}/${local.bench_endpoint_state_key}"
      },
      {
        Sid      = "BenchEndpointStateLock"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "arn:aws:s3:::${var.state_bucket}/${local.bench_endpoint_state_key}.tflock"
      },
      # A terraform run that cannot push the state it changed leaves errored.tfstate in the
      # run's temporary copy of the stack; the run copies it here, keyed by run id and
      # command, for `terraform state push`. Write only: recovery reads it as the operator.
      # The prefix is _ERRORED_STATE_PREFIX in the orchestration package's bench_endpoint.py.
      {
        Sid      = "BenchEndpointErroredState"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "arn:aws:s3:::${var.state_bucket}/bench-endpoint/errored/*"
      },
      # The stack reads the bootstrap outputs (the bench-client image and results bucket)
      # through terraform_remote_state; read-only.
      {
        Sid      = "BootstrapStateRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.state_bucket}/bootstrap/terraform.tfstate"
      },
      # The S3 backend lists the bucket's keys at init to find workspaces. Listing returns
      # key names, not state contents.
      {
        Sid      = "BenchEndpointStateList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.state_bucket}"
      },
      # The bench host instance launches only carrying the bench-endpoint tag.
      {
        Sid       = "BenchEndpointLaunch"
        Effect    = "Allow"
        Action    = ["ec2:RunInstances"]
        Resource  = "${local.bench_endpoint_ec2_arn_prefix}:instance/*"
        Condition = { StringEquals = { "aws:RequestTag/Component" = local.bench_endpoint_component_tag } }
      },
      # RunInstances is also authorized on what the instance launches with. The AMI is
      # Amazon's and the subnet the cluster's, so neither carries the bench-endpoint tag;
      # the volume and interface are created by the launch itself, untagged, and go with
      # the instance on terminate. The host's security group is listed here so one
      # statement covers every launch input.
      {
        Sid    = "BenchEndpointLaunchInputs"
        Effect = "Allow"
        Action = ["ec2:RunInstances"]
        Resource = [
          "arn:aws:ec2:${var.region}::image/*",
          "${local.bench_endpoint_ec2_arn_prefix}:subnet/*",
          "${local.bench_endpoint_ec2_arn_prefix}:network-interface/*",
          "${local.bench_endpoint_ec2_arn_prefix}:security-group/*",
          "${local.bench_endpoint_ec2_arn_prefix}:volume/*",
        ]
      },
      # The host and load balancer security groups are created only carrying the tag, and
      # only in a VPC tagged Project=slipstream, which the VPC module puts on the
      # cluster's VPC.
      {
        Sid       = "BenchEndpointCreateSecurityGroup"
        Effect    = "Allow"
        Action    = ["ec2:CreateSecurityGroup"]
        Resource  = "${local.bench_endpoint_ec2_arn_prefix}:security-group/*"
        Condition = { StringEquals = { "aws:RequestTag/Component" = local.bench_endpoint_component_tag } }
      },
      {
        Sid       = "BenchEndpointSecurityGroupVpc"
        Effect    = "Allow"
        Action    = ["ec2:CreateSecurityGroup"]
        Resource  = "${local.bench_endpoint_ec2_arn_prefix}:vpc/*"
        Condition = { StringEquals = { "aws:ResourceTag/Project" = local.tags.Project } }
      },
      # EC2 tags are written only as part of creating a resource, never added to an
      # existing one, so the worker cannot tag a foreign EC2 resource into its own scope.
      {
        Sid    = "BenchEndpointTagOnCreate"
        Effect = "Allow"
        Action = ["ec2:CreateTags"]
        Resource = [
          "${local.bench_endpoint_ec2_arn_prefix}:instance/*",
          "${local.bench_endpoint_ec2_arn_prefix}:volume/*",
          "${local.bench_endpoint_ec2_arn_prefix}:network-interface/*",
          "${local.bench_endpoint_ec2_arn_prefix}:security-group/*",
          "${local.bench_endpoint_ec2_arn_prefix}:security-group-rule/*",
        ]
        Condition = {
          StringEquals = {
            "ec2:CreateAction" = [
              "RunInstances",
              "CreateSecurityGroup",
              "AuthorizeSecurityGroupIngress",
              "AuthorizeSecurityGroupEgress",
            ]
          }
        }
      },
      # Terminate or modify the host, delete its security groups, and manage their rules
      # only where the resource carries the bench-endpoint tag. A rule is modified in place
      # when a replaced host's public IP moves the load balancer's ingress rule, which
      # authorizes against the rule as well as its group.
      {
        Sid    = "BenchEndpointManageEc2"
        Effect = "Allow"
        Action = [
          "ec2:TerminateInstances",
          "ec2:ModifyInstanceAttribute",
          "ec2:DeleteSecurityGroup",
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:AuthorizeSecurityGroupEgress",
          "ec2:RevokeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupEgress",
          "ec2:ModifySecurityGroupRules",
        ]
        Resource = [
          "${local.bench_endpoint_ec2_arn_prefix}:instance/*",
          "${local.bench_endpoint_ec2_arn_prefix}:security-group/*",
          "${local.bench_endpoint_ec2_arn_prefix}:security-group-rule/*",
        ]
        Condition = { StringEquals = { "aws:ResourceTag/Component" = local.bench_endpoint_component_tag } }
      },
      # A rule created with tags is itself a resource the authorize is checked against;
      # it exists only once created, so it is scoped by the tag it is created with.
      {
        Sid    = "BenchEndpointSecurityGroupRules"
        Effect = "Allow"
        Action = [
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:AuthorizeSecurityGroupEgress",
        ]
        Resource  = "${local.bench_endpoint_ec2_arn_prefix}:security-group-rule/*"
        Condition = { StringEquals = { "aws:RequestTag/Component" = local.bench_endpoint_component_tag } }
      },
      # The load balancer's NodePort rule lands on the eks node security group, the one
      # group carrying this cluster's Karpenter discovery tag. Ingress only, but IAM has no
      # condition on port or source, so any ingress rule may be opened or revoked there.
      {
        Sid    = "BenchEndpointNodeIngress"
        Effect = "Allow"
        Action = [
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupIngress",
        ]
        Resource  = "${local.bench_endpoint_ec2_arn_prefix}:security-group/*"
        Condition = { StringEquals = { "aws:ResourceTag/karpenter.sh/discovery" = var.cluster_name } }
      },
      # The load balancer, target group, listener, and trust store, by the bench- prefix.
      {
        Sid    = "BenchEndpointLoadBalancer"
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:CreateLoadBalancer",
          "elasticloadbalancing:CreateTargetGroup",
          "elasticloadbalancing:CreateListener",
          "elasticloadbalancing:CreateTrustStore",
          "elasticloadbalancing:DeleteLoadBalancer",
          "elasticloadbalancing:DeleteTargetGroup",
          "elasticloadbalancing:DeleteListener",
          "elasticloadbalancing:DeleteTrustStore",
          "elasticloadbalancing:ModifyLoadBalancerAttributes",
          "elasticloadbalancing:ModifyTargetGroup",
          "elasticloadbalancing:ModifyTargetGroupAttributes",
          "elasticloadbalancing:ModifyListener",
          "elasticloadbalancing:ModifyListenerAttributes",
          "elasticloadbalancing:ModifyTrustStore",
          "elasticloadbalancing:SetSecurityGroups",
          "elasticloadbalancing:SetSubnets",
          "elasticloadbalancing:AddTags",
          "elasticloadbalancing:RemoveTags",
        ]
        Resource = [
          "${local.bench_endpoint_elb_arn_prefix}:loadbalancer/app/bench-*",
          "${local.bench_endpoint_elb_arn_prefix}:targetgroup/bench-*",
          "${local.bench_endpoint_elb_arn_prefix}:listener/app/bench-*",
          "${local.bench_endpoint_elb_arn_prefix}:truststore/bench-*",
        ]
      },
      # Attach the target group to this cluster's node autoscaling groups, which EKS tags
      # with the cluster name. Both the classic target-group API and the traffic-source
      # API that supersedes it.
      {
        Sid    = "BenchEndpointTargetGroupAttachment"
        Effect = "Allow"
        Action = [
          "autoscaling:AttachLoadBalancerTargetGroups",
          "autoscaling:DetachLoadBalancerTargetGroups",
          "autoscaling:AttachTrafficSources",
          "autoscaling:DetachTrafficSources",
        ]
        Resource  = "arn:aws:autoscaling:${var.region}:${data.aws_caller_identity.current.account_id}:autoScalingGroup:*:autoScalingGroupName/*"
        Condition = { StringEquals = { "aws:ResourceTag/eks:cluster-name" = var.cluster_name } }
      },
      # The listener's server certificate: imported only carrying the tag, then read and
      # deleted only where it carries it. Importing with tags needs AddTagsToCertificate,
      # and ACM offers no condition tying it to the import, so the worker can also tag an
      # existing certificate into this scope: the tag is a bound on intent, not a hard
      # boundary, for ACM.
      {
        Sid       = "BenchEndpointCertificateImport"
        Effect    = "Allow"
        Action    = ["acm:ImportCertificate", "acm:AddTagsToCertificate"]
        Resource  = "arn:aws:acm:${var.region}:${data.aws_caller_identity.current.account_id}:certificate/*"
        Condition = { StringEquals = { "aws:RequestTag/Component" = local.bench_endpoint_component_tag } }
      },
      {
        Sid    = "BenchEndpointCertificateManage"
        Effect = "Allow"
        Action = [
          "acm:DeleteCertificate",
          "acm:DescribeCertificate",
          "acm:GetCertificate",
          "acm:ListTagsForCertificate",
        ]
        Resource  = "arn:aws:acm:${var.region}:${data.aws_caller_identity.current.account_id}:certificate/*"
        Condition = { StringEquals = { "aws:ResourceTag/Component" = local.bench_endpoint_component_tag } }
      },
      # The bench-client secret, by the name prefix the bench host's read grant also uses.
      {
        Sid    = "BenchEndpointClientSecret"
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:DeleteSecret",
          "secretsmanager:DescribeSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:GetResourcePolicy",
          "secretsmanager:TagResource",
        ]
        Resource = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${local.bench_client_secret_prefix}*"
      },
      # The trust-store bucket, by its name prefix. Terraform reads back each of the
      # bucket's configuration subresources on refresh, hence the Get set.
      {
        Sid    = "BenchEndpointTrustStoreBucket"
        Effect = "Allow"
        Action = [
          "s3:CreateBucket",
          "s3:DeleteBucket",
          "s3:ListBucket",
          "s3:ListBucketVersions",
          "s3:GetBucket*",
          "s3:GetAccelerateConfiguration",
          "s3:GetLifecycleConfiguration",
          "s3:GetReplicationConfiguration",
          "s3:GetEncryptionConfiguration",
          "s3:PutBucketTagging",
          "s3:PutBucketPublicAccessBlock",
        ]
        Resource = local.bench_endpoint_trust_store
      },
      # The CA bundle object in that bucket; the load balancer reads it as the caller when
      # the trust store is created.
      {
        Sid    = "BenchEndpointTrustStoreObjects"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:GetObjectTagging",
          "s3:DeleteObject",
          "s3:DeleteObjectVersion",
        ]
        Resource = "${local.bench_endpoint_trust_store}/*"
      },
    ]
  })
}

# The stack publishes the vLLM api-key to the bench client, so the worker reads it from the
# cluster Secret at run time. A get on that one Secret by name, nothing wider; a separate
# Role from the vllm-gpu redeploy grant (worker.tf) so each reads as one purpose.
resource "kubernetes_role_v1" "vllm_api_key_read" {
  metadata {
    name      = "vllm-api-key-read"
    namespace = local.vllm_namespace
  }

  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    verbs          = ["get"]
    resource_names = ["vllm-api-key"]
  }

  depends_on = [kubernetes_namespace_v1.slipstream]
}

resource "kubernetes_role_binding_v1" "vllm_api_key_read" {
  metadata {
    name      = "prefect-worker-vllm-api-key-read"
    namespace = local.vllm_namespace
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.vllm_api_key_read.metadata[0].name
  }

  # Cross-namespace subject: the worker's SA lives in prefect, the Secret in slipstream.
  subject {
    kind      = "ServiceAccount"
    name      = local.worker_service_account
    namespace = local.prefect_namespace
  }
}

# The topology as a ConfigMap the worker loads into its environment (worker.extraEnvVarsCMs
# in worker.tf). A ConfigMap rather than inline Helm env, so the module ids, unknown until
# the cluster exists, stay out of the release's values and those values stay known at plan.
# Pod env is read at start: the release references the ConfigMap so the worker never
# starts without it, and a change to it reaches the worker only when its pod restarts.
resource "kubernetes_config_map_v1" "bench_endpoint_topology" {
  metadata {
    name      = "bench-endpoint-topology"
    namespace = local.prefect_namespace
  }

  data = local.bench_endpoint_topology

  # The prefect namespace is created by the server release.
  depends_on = [helm_release.prefect_server]
}
