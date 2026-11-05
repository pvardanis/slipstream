# Plan-level tests for the Prefect worker's rights to apply and destroy the bench-endpoint
# stack (#470): iam:PassRole on the bench host role and no IAM create, attach, or put;
# create/delete rights scoped by the bench-endpoint name prefix or tag; state access
# limited to the bench-endpoint key; a get on the one vLLM api-key Secret; and the
# cluster topology in the worker's environment. They run offline — the aws, helm, and
# kubernetes providers are mocked and the bootstrap remote state is stubbed — so the
# assertions check the grants' shape without a cluster (ADR-0002 tier-1). A flow run
# actually applying and destroying the stack is the live e2e tier.
#
# The policy is jsonencode()'d inline from values known at plan (names, tags, the
# account id), so its statements are assertable here.

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
  mock_data "aws_ecr_image" {
    defaults = { image_tags = ["main", "orchestrator-abc123"] }
  }
}

mock_provider "aws" {
  alias = "virginia"
}
mock_provider "helm" {}
mock_provider "kubernetes" {}

override_data {
  target = data.terraform_remote_state.bootstrap
  values = {
    outputs = {
      orchestration_image_repo_url    = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
      bench_image_repo_arn            = "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream-bench"
      bench_results_bucket_arn        = "arn:aws:s3:::slipstream-bench-results-abc123"
      sweep_dispatch_role_arn         = "arn:aws:iam::111122223333:role/slipstream-sweep-dispatch"
      grafana_cloud_write_secret_name = "grafana-cloud/write"
      cluster_name                    = "slipstream"
      prefect_namespace               = "prefect"
    }
  }
}

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

run "worker_bench_endpoint_rights" {
  command = plan

  # --- Policy shape: exactly these statements, all plain Allows ---

  # The statement set is pinned, so a widening added as a new statement fails here rather
  # than slipping past the per-statement checks below, which only pin what is there. Sids
  # are unique, so no check below can be satisfied by one copy while a second widens.
  assert {
    condition = length(jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement) == 22 && toset([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s.Sid]) == toset([
      "BenchHostPassRole",
      "BenchEndpointRead",
      "BenchEndpointAmiParameter",
      "BenchEndpointState",
      "BenchEndpointStateLock",
      "BootstrapStateRead",
      "BenchEndpointStateList",
      "BenchEndpointLaunch",
      "BenchEndpointLaunchInputs",
      "BenchEndpointCreateSecurityGroup",
      "BenchEndpointSecurityGroupVpc",
      "BenchEndpointTagOnCreate",
      "BenchEndpointManageEc2",
      "BenchEndpointSecurityGroupRules",
      "BenchEndpointNodeIngress",
      "BenchEndpointLoadBalancer",
      "BenchEndpointTargetGroupAttachment",
      "BenchEndpointCertificateImport",
      "BenchEndpointCertificateManage",
      "BenchEndpointClientSecret",
      "BenchEndpointTrustStoreBucket",
      "BenchEndpointTrustStoreObjects",
    ])
    error_message = "The bench-endpoint policy must hold exactly the 22 expected statements, each Sid once."
  }
  # Across both worker policies: Allow only, and no NotAction/NotResource, which would
  # grant everything outside a list the checks below never see.
  assert {
    condition = alltrue([
      for s in concat(
        jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement,
        jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement,
      ) : s.Effect == "Allow" && !contains(keys(s), "NotAction") && !contains(keys(s), "NotResource")
    ])
    error_message = "The worker's policies must be plain Allow statements with no NotAction or NotResource."
  }
  # Wildcard actions are limited to the read-only refresh set; IAM matches action names
  # case-insensitively, so the comparison is too.
  assert {
    condition = length([
      for a in flatten([
        for s in concat(
          jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement,
          jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement,
        ) : try(tolist(s.Action), [s.Action])
      ]) : a if strcontains(a, "*") && !contains(["ec2:describe*", "elasticloadbalancing:describe*", "autoscaling:describe*", "s3:getbucket*"], lower(a))
    ]) == 0
    error_message = "The only wildcard actions the worker may hold are the Describe refresh set and s3:GetBucket*."
  }
  # A bare "*" resource only on the refresh reads, which take no resource scope; those
  # reads are pinned to exactly the three Describe wildcards.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if contains(try(tolist(s.Resource), [s.Resource]), "*") && s.Sid != "BenchEndpointRead"]) == 0
    error_message = "Only the BenchEndpointRead statement may name a bare \"*\" resource."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointRead" && toset(try(tolist(s.Action), [s.Action])) == toset(["ec2:Describe*", "elasticloadbalancing:Describe*", "autoscaling:Describe*"])]) == 1
    error_message = "The unscoped read statement must grant only the three Describe wildcards."
  }

  # IAM caps a role's inline policies at 10,240 characters in aggregate (whitespace not
  # counted), so the two worker policies together must fit or the apply fails at AWS.
  assert {
    condition     = length(jsonencode(jsondecode(aws_iam_role_policy.prefect_worker.policy))) + length(jsonencode(jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy))) < 10240
    error_message = "The worker's inline policies must fit IAM's 10,240-character aggregate limit."
  }

  # --- IAM: pass the bench host role, create none ---

  # Every iam:PassRole across both worker policies targets exactly the bench host role,
  # toward EC2 only: PassRole on any other role plus RunInstances is a path to admin.
  assert {
    condition = toset(flatten([
      for s in concat(
        jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement,
        jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement,
      ) : try(tolist(s.Resource), [s.Resource]) if contains([for a in try(tolist(s.Action), [s.Action]) : lower(a)], "iam:passrole")
    ])) == toset(["arn:aws:iam::111122223333:role/slipstream-bench-host"])
    error_message = "iam:PassRole must target the bench host role and no other."
  }
  assert {
    condition = alltrue([
      for s in concat(
        jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement,
        jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement,
      ) : try(s.Condition.StringEquals["iam:PassedToService"], null) == "ec2.amazonaws.com" if contains([for a in try(tolist(s.Action), [s.Action]) : lower(a)], "iam:passrole")
    ])
    error_message = "Every iam:PassRole grant must be conditioned on EC2 as the passed-to service."
  }
  # No other IAM action anywhere: no role or policy create, attach, or put, so the flow's
  # infra rights cannot be escalated to admin.
  assert {
    condition = length([
      for a in flatten([
        for s in concat(
          jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement,
          jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement,
        ) : try(tolist(s.Action), [s.Action])
      ]) : a if a == "*" || (startswith(lower(a), "iam:") && lower(a) != "iam:passrole")
    ]) == 0
    error_message = "The worker's policies must grant no IAM action other than iam:PassRole."
  }

  # --- Every grant is prefix- or tag-scoped ---

  # Each statement other than the unscoped reads and the launch inputs is narrowed either
  # by a StringEquals condition on a known scoping key with a literal value, or by every
  # resource matching one of the stack's exact name-prefix ARNs.
  assert {
    condition = length([
      for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s.Sid
      if !contains(["BenchEndpointRead", "BenchEndpointLaunchInputs"], s.Sid) && !(
        try(
          toset(keys(s.Condition)) == toset(["StringEquals"]) && alltrue([
            for k, v in s.Condition.StringEquals : contains([
              "aws:RequestTag/Component",
              "aws:ResourceTag/Component",
              "aws:ResourceTag/karpenter.sh/discovery",
              "aws:ResourceTag/eks:cluster-name",
              "aws:ResourceTag/Project",
              "ec2:CreateAction",
              "iam:PassedToService",
            ], k) && length([for x in try(tolist(v), [v]) : x if strcontains(x, "*")]) == 0
          ]),
          false
          ) || alltrue([
            for r in try(tolist(s.Resource), [s.Resource]) : anytrue([
              for p in [
                "^arn:aws:ssm:eu-west-1::parameter/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64$",
                "^arn:aws:s3:::slipstream-tfstate-abc123(/bench-endpoint/terraform\\.tfstate(\\.tflock)?|/bootstrap/terraform\\.tfstate)?$",
                "^arn:aws:elasticloadbalancing:eu-west-1:111122223333:(loadbalancer/app|targetgroup|listener/app|truststore)/bench-\\*$",
                "^arn:aws:secretsmanager:eu-west-1:111122223333:secret:slipstream-bench-endpoint-client-\\*$",
                "^arn:aws:s3:::slipstream-bench-endpoint-truststore-\\*(/\\*)?$",
              ] : can(regex(p, r))
            ])
        ])
      )
    ]) == 0
    error_message = "Every grant must be scoped by a tag condition or by the stack's exact name-prefix ARNs."
  }
  # The launch inputs are the one unconditioned, untagged grant: RunInstances only, on
  # exactly the AMI, subnet, interface, security group, and volume it launches with. The
  # instance is not among them, so its tag condition below cannot be bypassed.
  assert {
    condition = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointLaunchInputs" && toset(try(tolist(s.Action), [s.Action])) == toset(["ec2:RunInstances"]) && toset(try(tolist(s.Resource), [s.Resource])) == toset([
      "arn:aws:ec2:eu-west-1::image/*",
      "arn:aws:ec2:eu-west-1:111122223333:subnet/*",
      "arn:aws:ec2:eu-west-1:111122223333:network-interface/*",
      "arn:aws:ec2:eu-west-1:111122223333:security-group/*",
      "arn:aws:ec2:eu-west-1:111122223333:volume/*",
    ])]) == 1
    error_message = "The launch-inputs statement must grant only RunInstances on the five launch-input resource types."
  }
  # The instance is launched only carrying the bench-endpoint Component tag.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointLaunch" && toset(try(tolist(s.Action), [s.Action])) == toset(["ec2:RunInstances"]) && s.Resource == "arn:aws:ec2:eu-west-1:111122223333:instance/*" && try(s.Condition.StringEquals["aws:RequestTag/Component"], null) == "bench-endpoint"]) == 1
    error_message = "RunInstances on the instance must require the Component=bench-endpoint request tag."
  }
  # Security groups and their tagged rules are created only carrying the tag.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if contains(["BenchEndpointCreateSecurityGroup", "BenchEndpointSecurityGroupRules"], s.Sid) && try(s.Condition.StringEquals["aws:RequestTag/Component"], null) == "bench-endpoint"]) == 2
    error_message = "Security groups and their rules must be created only carrying the Component=bench-endpoint request tag."
  }
  # EC2 tags are written only as part of a create, so a foreign EC2 resource can never be
  # tagged into the worker's scope.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointTagOnCreate" && toset(try(tolist(s.Action), [s.Action])) == toset(["ec2:CreateTags"]) && toset(try(s.Condition.StringEquals["ec2:CreateAction"], [])) == toset(["RunInstances", "CreateSecurityGroup", "AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress"])]) == 1
    error_message = "ec2:CreateTags must be limited to tagging at create time by ec2:CreateAction."
  }
  # Terminating an instance, deleting a security group, or changing a rule needs the
  # resource to carry the bench-endpoint tag, so the worker never touches another workload.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointManageEc2" && contains(try(tolist(s.Action), [s.Action]), "ec2:TerminateInstances") && contains(try(tolist(s.Action), [s.Action]), "ec2:DeleteSecurityGroup") && contains(try(tolist(s.Action), [s.Action]), "ec2:ModifySecurityGroupRules") && contains(try(tolist(s.Resource), [s.Resource]), "arn:aws:ec2:eu-west-1:111122223333:security-group-rule/*") && try(s.Condition.StringEquals["aws:ResourceTag/Component"], null) == "bench-endpoint"]) == 1
    error_message = "Terminate, delete, and rule changes must require the Component=bench-endpoint resource tag, rules included."
  }
  # The NodePort rule lands on the eks node security group, found by its discovery tag,
  # and only ingress may be opened or revoked there.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointNodeIngress" && toset(try(tolist(s.Action), [s.Action])) == toset(["ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress"]) && try(s.Condition.StringEquals["aws:ResourceTag/karpenter.sh/discovery"], null) == "slipstream"]) == 1
    error_message = "The node security group rule grant must be ingress authorize/revoke only, scoped by the cluster's discovery tag."
  }
  # The target group attaches only to this cluster's node autoscaling groups.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointTargetGroupAttachment" && contains(try(tolist(s.Action), [s.Action]), "autoscaling:AttachLoadBalancerTargetGroups") && try(s.Condition.StringEquals["aws:ResourceTag/eks:cluster-name"], null) == "slipstream"]) == 1
    error_message = "The target group attachment must be scoped to the cluster's node autoscaling groups by their eks:cluster-name tag."
  }
  # The load balancer, target group, listener, and trust store are scoped by the
  # stack's bench- name prefix.
  assert {
    condition = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointLoadBalancer" && contains(try(tolist(s.Action), [s.Action]), "elasticloadbalancing:CreateLoadBalancer") && toset(try(tolist(s.Resource), [s.Resource])) == toset([
      "arn:aws:elasticloadbalancing:eu-west-1:111122223333:loadbalancer/app/bench-*",
      "arn:aws:elasticloadbalancing:eu-west-1:111122223333:targetgroup/bench-*",
      "arn:aws:elasticloadbalancing:eu-west-1:111122223333:listener/app/bench-*",
      "arn:aws:elasticloadbalancing:eu-west-1:111122223333:truststore/bench-*",
    ])]) == 1
    error_message = "The load balancer grants must be scoped to the bench- name prefix."
  }
  # The client secret is scoped by its name prefix.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointClientSecret" && contains(try(tolist(s.Action), [s.Action]), "secretsmanager:CreateSecret") && s.Resource == "arn:aws:secretsmanager:eu-west-1:111122223333:secret:slipstream-bench-endpoint-client-*"]) == 1
    error_message = "The Secrets Manager grant must be scoped to the bench-client secret name prefix."
  }
  # The ACM certificate is imported only carrying the bench-endpoint tag and deleted only
  # when it carries it.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointCertificateImport" && contains(try(tolist(s.Action), [s.Action]), "acm:ImportCertificate") && try(s.Condition.StringEquals["aws:RequestTag/Component"], null) == "bench-endpoint"]) == 1
    error_message = "ImportCertificate must require the Component=bench-endpoint request tag."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointCertificateManage" && contains(try(tolist(s.Action), [s.Action]), "acm:DeleteCertificate") && try(s.Condition.StringEquals["aws:ResourceTag/Component"], null) == "bench-endpoint"]) == 1
    error_message = "DeleteCertificate must require the Component=bench-endpoint resource tag."
  }

  # --- S3: the results bucket, the trust-store bucket, and the bench-endpoint state key ---

  # Every S3 resource across both worker policies, outside the results bucket (pinned in
  # worker.tftest.hcl), is one of these: the state bucket for listing, the bench-endpoint
  # state key and lockfile, the bootstrap state read, and the trust-store bucket by prefix.
  # A glob over the state bucket, another key, or another bucket shows up as a mismatch.
  assert {
    condition = toset(flatten([
      for s in concat(
        jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement,
        jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement,
        ) : [
        for r in try(tolist(s.Resource), [s.Resource]) : r if startswith(r, "arn:aws:s3:::") && split("/", r)[0] != "arn:aws:s3:::slipstream-bench-results-abc123"
      ]
      ])) == toset([
      "arn:aws:s3:::slipstream-tfstate-abc123",
      "arn:aws:s3:::slipstream-tfstate-abc123/bench-endpoint/terraform.tfstate",
      "arn:aws:s3:::slipstream-tfstate-abc123/bench-endpoint/terraform.tfstate.tflock",
      "arn:aws:s3:::slipstream-tfstate-abc123/bootstrap/terraform.tfstate",
      "arn:aws:s3:::slipstream-bench-endpoint-truststore-*",
      "arn:aws:s3:::slipstream-bench-endpoint-truststore-*/*",
    ])
    error_message = "S3 access outside the results bucket must be limited to the bench-endpoint state key, its lockfile, the bootstrap state read, the state bucket listing, and the trust-store bucket."
  }
  # Each state grant's actions are exact: read/write the state, read/write/delete the
  # lockfile, read the bootstrap state, list the bucket.
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : (
        s.Sid == "BenchEndpointState" ? toset(try(tolist(s.Action), [s.Action])) == toset(["s3:GetObject", "s3:PutObject"]) && toset(try(tolist(s.Resource), [s.Resource])) == toset(["arn:aws:s3:::slipstream-tfstate-abc123/bench-endpoint/terraform.tfstate"]) :
        s.Sid == "BenchEndpointStateLock" ? toset(try(tolist(s.Action), [s.Action])) == toset(["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]) && toset(try(tolist(s.Resource), [s.Resource])) == toset(["arn:aws:s3:::slipstream-tfstate-abc123/bench-endpoint/terraform.tfstate.tflock"]) :
        s.Sid == "BootstrapStateRead" ? toset(try(tolist(s.Action), [s.Action])) == toset(["s3:GetObject"]) && toset(try(tolist(s.Resource), [s.Resource])) == toset(["arn:aws:s3:::slipstream-tfstate-abc123/bootstrap/terraform.tfstate"]) :
        s.Sid == "BenchEndpointStateList" ? toset(try(tolist(s.Action), [s.Action])) == toset(["s3:ListBucket"]) && toset(try(tolist(s.Resource), [s.Resource])) == toset(["arn:aws:s3:::slipstream-tfstate-abc123"]) :
        true
      )
    ])
    error_message = "The state grants must be exactly: read/write the bench-endpoint state, read/write/delete its lockfile, read the bootstrap state, list the bucket."
  }
  # The trust-store bucket is created and deleted by its name prefix.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointTrustStoreBucket" && contains(try(tolist(s.Action), [s.Action]), "s3:CreateBucket") && s.Resource == "arn:aws:s3:::slipstream-bench-endpoint-truststore-*"]) == 1
    error_message = "The trust-store bucket grant must be scoped to its name prefix."
  }

  # --- RBAC: get on the one api-key Secret ---

  assert {
    condition     = kubernetes_role_v1.vllm_api_key_read.metadata[0].namespace == "slipstream"
    error_message = "The api-key read Role must live in the slipstream namespace with the Secret."
  }
  assert {
    condition     = length(kubernetes_role_v1.vllm_api_key_read.rule) == 1 && toset(kubernetes_role_v1.vllm_api_key_read.rule[0].api_groups) == toset([""]) && toset(kubernetes_role_v1.vllm_api_key_read.rule[0].resources) == toset(["secrets"]) && toset(kubernetes_role_v1.vllm_api_key_read.rule[0].verbs) == toset(["get"]) && toset(kubernetes_role_v1.vllm_api_key_read.rule[0].resource_names) == toset(["vllm-api-key"])
    error_message = "The api-key read Role must grant get on the core vllm-api-key Secret and nothing else."
  }
  assert {
    condition     = kubernetes_role_binding_v1.vllm_api_key_read.metadata[0].namespace == "slipstream" && kubernetes_role_binding_v1.vllm_api_key_read.role_ref[0].kind == "Role" && kubernetes_role_binding_v1.vllm_api_key_read.role_ref[0].name == kubernetes_role_v1.vllm_api_key_read.metadata[0].name
    error_message = "The binding must sit in the slipstream namespace and reference the api-key read Role, not a ClusterRole."
  }
  assert {
    condition     = length(kubernetes_role_binding_v1.vllm_api_key_read.subject) == 1 && kubernetes_role_binding_v1.vllm_api_key_read.subject[0].kind == "ServiceAccount" && kubernetes_role_binding_v1.vllm_api_key_read.subject[0].name == "prefect-worker" && kubernetes_role_binding_v1.vllm_api_key_read.subject[0].namespace == "prefect"
    error_message = "The api-key read Role must be bound to the worker's ServiceAccount alone."
  }
}

# The worker's environment carries the topology the stack is applied with. The VPC and eks
# modules' outputs are unknown at plan under the mock provider, so they are overridden
# with known values here — distinct public and private subnets, and two node groups — so
# the ConfigMap's values, not only its keys, are checked.
run "worker_bench_endpoint_topology" {
  command = plan

  override_module {
    target = module.vpc
    outputs = {
      vpc_id          = "vpc-0abc"
      public_subnets  = ["subnet-pub-a", "subnet-pub-b"]
      private_subnets = ["subnet-priv-a", "subnet-priv-b"]
    }
  }
  override_module {
    target = module.eks
    outputs = {
      cluster_name              = "slipstream"
      node_security_group_id    = "sg-0node"
      cluster_security_group_id = "sg-0cluster"
      eks_managed_node_groups = {
        cpu = { node_group_autoscaling_group_names = ["eks-cpu-asg-1"] }
        gpu = { node_group_autoscaling_group_names = ["eks-gpu-asg-1", "eks-gpu-asg-2"] }
      }
    }
  }

  assert {
    condition = toset(keys(kubernetes_config_map_v1.bench_endpoint_topology.data)) == toset([
      "BENCH_ENDPOINT_STATE_BUCKET",
      "BENCH_ENDPOINT_VPC_ID",
      "BENCH_ENDPOINT_PUBLIC_SUBNETS",
      "BENCH_ENDPOINT_NODE_SECURITY_GROUP_ID",
      "BENCH_ENDPOINT_NODE_AUTOSCALING_GROUPS",
      "BENCH_ENDPOINT_INSTANCE_PROFILE",
    ])
    error_message = "The topology ConfigMap must carry the state bucket, VPC, public subnets, node security group, node autoscaling groups, and bench host instance profile."
  }
  assert {
    condition     = kubernetes_config_map_v1.bench_endpoint_topology.metadata[0].namespace == "prefect"
    error_message = "The topology ConfigMap must live in the worker's namespace."
  }
  assert {
    condition     = kubernetes_config_map_v1.bench_endpoint_topology.data["BENCH_ENDPOINT_STATE_BUCKET"] == "slipstream-tfstate-abc123"
    error_message = "The worker's state bucket must be the stack's state bucket."
  }
  assert {
    condition     = kubernetes_config_map_v1.bench_endpoint_topology.data["BENCH_ENDPOINT_VPC_ID"] == "vpc-0abc"
    error_message = "The worker's VPC must be the cluster VPC."
  }
  # The load balancer and bench host launch into internet-routable subnets.
  assert {
    condition     = jsondecode(kubernetes_config_map_v1.bench_endpoint_topology.data["BENCH_ENDPOINT_PUBLIC_SUBNETS"]) == ["subnet-pub-a", "subnet-pub-b"]
    error_message = "The worker's subnets must be the cluster's public subnets, as a JSON list."
  }
  assert {
    condition     = kubernetes_config_map_v1.bench_endpoint_topology.data["BENCH_ENDPOINT_NODE_SECURITY_GROUP_ID"] == "sg-0node"
    error_message = "The worker's node security group must be the node group's, not the cluster's."
  }
  # A flat JSON list of strings, the shape the stack's list(string) variable takes.
  assert {
    condition     = jsondecode(kubernetes_config_map_v1.bench_endpoint_topology.data["BENCH_ENDPOINT_NODE_AUTOSCALING_GROUPS"]) == ["eks-cpu-asg-1", "eks-gpu-asg-1", "eks-gpu-asg-2"]
    error_message = "The worker's node autoscaling groups must be every node group's ASGs, as a flat JSON list."
  }
  assert {
    condition     = kubernetes_config_map_v1.bench_endpoint_topology.data["BENCH_ENDPOINT_INSTANCE_PROFILE"] == "slipstream-bench-host"
    error_message = "The worker must launch the bench host under the eks-owned instance profile."
  }
  assert {
    condition     = yamldecode(helm_release.prefect_worker.values[0]).worker.extraEnvVarsCMs == [kubernetes_config_map_v1.bench_endpoint_topology.metadata[0].name]
    error_message = "The worker must load the topology ConfigMap into its environment."
  }
}
