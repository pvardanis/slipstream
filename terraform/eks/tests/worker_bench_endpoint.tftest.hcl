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

  # --- IAM: pass the bench host role, create none ---

  # The worker launches the bench host under the eks-owned instance profile, which needs
  # iam:PassRole on exactly that role, and only toward EC2.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchHostPassRole" && s.Effect == "Allow" && toset(try(tolist(s.Action), [s.Action])) == toset(["iam:PassRole"]) && s.Resource == "arn:aws:iam::111122223333:role/slipstream-bench-host" && try(s.Condition.StringEquals["iam:PassedToService"], null) == "ec2.amazonaws.com"]) == 1
    error_message = "The worker must hold iam:PassRole on the bench host role only, conditioned on EC2 as the passed-to service."
  }
  # No other IAM action anywhere in the worker's policies: no role or policy create,
  # attach, or put, so the flow's infra rights cannot be escalated to admin.
  assert {
    condition = length([
      for a in flatten([
        for s in concat(
          jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement,
          jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement,
        ) : try(tolist(s.Action), [s.Action])
      ]) : a if startswith(a, "iam:") && a != "iam:PassRole"
    ]) == 0
    error_message = "The worker's policies must grant no IAM action other than iam:PassRole."
  }

  # IAM caps a role's inline policies at 10,240 characters in aggregate (whitespace not
  # counted), so the two worker policies together must fit or the apply fails at AWS.
  assert {
    condition     = length(jsonencode(jsondecode(aws_iam_role_policy.prefect_worker.policy))) + length(jsonencode(jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy))) < 10240
    error_message = "The worker's inline policies must fit IAM's 10,240-character aggregate limit."
  }

  # --- Create/delete rights are prefix- or tag-scoped ---

  # Every statement that creates, deletes, launches, terminates, imports, or opens a rule
  # names no bare "*" resource and is narrowed by a condition or a bench-endpoint name in
  # every resource ARN. BenchEndpointLaunchInputs is the one exception: RunInstances must
  # also be authorized on the AMI, subnet, network interface, security group, and volume it
  # launches with, which carry no bench-endpoint tag; the instance itself is tag-scoped.
  assert {
    condition = length([
      for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s
      if s.Sid != "BenchEndpointLaunchInputs" && length([
        for a in try(tolist(s.Action), [s.Action]) : a
        if can(regex(":(Create|Delete|RunInstances|Terminate|Import|Authorize|Revoke|Put|Attach|Detach)", a))
        ]) > 0 && (
        contains(try(tolist(s.Resource), [s.Resource]), "*") ||
        (try(s.Condition, null) == null && length([for r in try(tolist(s.Resource), [s.Resource]) : r if !can(regex("bench-endpoint|/bench-|:bench-|bench-host", r))]) > 0)
      )
    ]) == 0
    error_message = "Every create/delete statement must be scoped by a bench-endpoint name prefix or a tag condition, never a bare \"*\"."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointLaunchInputs" && toset(try(tolist(s.Action), [s.Action])) == toset(["ec2:RunInstances"])]) == 1
    error_message = "The unconditioned launch-inputs statement must grant ec2:RunInstances and nothing else."
  }
  # The instance is launched only carrying the bench-endpoint Component tag.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if contains(try(tolist(s.Action), [s.Action]), "ec2:RunInstances") && s.Resource == "arn:aws:ec2:eu-west-1:111122223333:instance/*" && try(s.Condition.StringEquals["aws:RequestTag/Component"], null) == "bench-endpoint"]) == 1
    error_message = "RunInstances on the instance must require the Component=bench-endpoint request tag."
  }
  # Terminating an instance or deleting a security group needs the resource to carry the
  # bench-endpoint tag, so the worker can never tear down another workload.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if contains(try(tolist(s.Action), [s.Action]), "ec2:TerminateInstances") && contains(try(tolist(s.Action), [s.Action]), "ec2:DeleteSecurityGroup") && try(s.Condition.StringEquals["aws:ResourceTag/Component"], null) == "bench-endpoint"]) == 1
    error_message = "TerminateInstances and DeleteSecurityGroup must require the Component=bench-endpoint resource tag."
  }
  # The NodePort rule lands on the eks node security group, found by its discovery tag,
  # and only ingress may be opened or revoked there.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Sid == "BenchEndpointNodeIngress" && toset(try(tolist(s.Action), [s.Action])) == toset(["ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress"]) && try(s.Condition.StringEquals["aws:ResourceTag/karpenter.sh/discovery"], null) == "slipstream"]) == 1
    error_message = "The node security group rule grant must be ingress authorize/revoke only, scoped by the cluster's discovery tag."
  }
  # The target group attaches only to this cluster's node autoscaling groups.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if contains(try(tolist(s.Action), [s.Action]), "autoscaling:AttachLoadBalancerTargetGroups") && try(s.Condition.StringEquals["aws:ResourceTag/eks:cluster-name"], null) == "slipstream"]) == 1
    error_message = "The target group attachment must be scoped to the cluster's node autoscaling groups by their eks:cluster-name tag."
  }
  # The load balancer, target group, listener, and trust store are scoped by the
  # stack's bench- name prefix.
  assert {
    condition = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if contains(try(tolist(s.Action), [s.Action]), "elasticloadbalancing:CreateLoadBalancer") && toset(try(tolist(s.Resource), [s.Resource])) == toset([
      "arn:aws:elasticloadbalancing:eu-west-1:111122223333:loadbalancer/app/bench-*",
      "arn:aws:elasticloadbalancing:eu-west-1:111122223333:targetgroup/bench-*",
      "arn:aws:elasticloadbalancing:eu-west-1:111122223333:listener/app/bench-*",
      "arn:aws:elasticloadbalancing:eu-west-1:111122223333:truststore/bench-*",
    ])]) == 1
    error_message = "The load balancer grants must be scoped to the bench- name prefix."
  }
  # The client secret and the trust-store bucket are scoped by their name prefixes.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if contains(try(tolist(s.Action), [s.Action]), "secretsmanager:CreateSecret") && s.Resource == "arn:aws:secretsmanager:eu-west-1:111122223333:secret:slipstream-bench-endpoint-client-*"]) == 1
    error_message = "The Secrets Manager grant must be scoped to the bench-client secret name prefix."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if contains(try(tolist(s.Action), [s.Action]), "s3:CreateBucket") && s.Resource == "arn:aws:s3:::slipstream-bench-endpoint-truststore-*"]) == 1
    error_message = "The trust-store bucket grant must be scoped to its name prefix."
  }
  # The ACM certificate is imported only carrying the bench-endpoint tag and deleted only
  # when it carries it.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if contains(try(tolist(s.Action), [s.Action]), "acm:ImportCertificate") && try(s.Condition.StringEquals["aws:RequestTag/Component"], null) == "bench-endpoint"]) == 1
    error_message = "ImportCertificate must require the Component=bench-endpoint request tag."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if contains(try(tolist(s.Action), [s.Action]), "acm:DeleteCertificate") && try(s.Condition.StringEquals["aws:ResourceTag/Component"], null) == "bench-endpoint"]) == 1
    error_message = "DeleteCertificate must require the Component=bench-endpoint resource tag."
  }

  # --- State: the bench-endpoint key only ---

  # Every S3 object grant on the state bucket targets the bench-endpoint state key or its
  # lockfile, except one read of the bootstrap state for the remote-state lookup.
  assert {
    condition = toset(flatten([
      for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : [
        for r in try(tolist(s.Resource), [s.Resource]) : r if startswith(r, "arn:aws:s3:::slipstream-tfstate-abc123/")
      ]
      ])) == toset([
      "arn:aws:s3:::slipstream-tfstate-abc123/bench-endpoint/terraform.tfstate",
      "arn:aws:s3:::slipstream-tfstate-abc123/bench-endpoint/terraform.tfstate.tflock",
      "arn:aws:s3:::slipstream-tfstate-abc123/bootstrap/terraform.tfstate",
    ])
    error_message = "State object access must be limited to the bench-endpoint key, its lockfile, and the bootstrap state read."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Resource == "arn:aws:s3:::slipstream-tfstate-abc123/bootstrap/terraform.tfstate" && toset(try(tolist(s.Action), [s.Action])) != toset(["s3:GetObject"])]) == 0
    error_message = "The bootstrap state must be read-only to the worker."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker_bench_endpoint.policy).Statement : s if s.Resource == "arn:aws:s3:::slipstream-tfstate-abc123/bench-endpoint/terraform.tfstate.tflock" && toset(try(tolist(s.Action), [s.Action])) == toset(["s3:GetObject", "s3:PutObject", "s3:DeleteObject"])]) == 1
    error_message = "The worker must read, write, and delete the bench-endpoint lockfile to take and release the state lock."
  }

  # --- RBAC: get on the one api-key Secret ---

  assert {
    condition     = kubernetes_role_v1.vllm_api_key_read.metadata[0].namespace == "slipstream"
    error_message = "The api-key read Role must live in the slipstream namespace with the Secret."
  }
  assert {
    condition     = length(kubernetes_role_v1.vllm_api_key_read.rule) == 1 && toset(kubernetes_role_v1.vllm_api_key_read.rule[0].resources) == toset(["secrets"]) && toset(kubernetes_role_v1.vllm_api_key_read.rule[0].verbs) == toset(["get"]) && toset(kubernetes_role_v1.vllm_api_key_read.rule[0].resource_names) == toset(["vllm-api-key"])
    error_message = "The api-key read Role must grant get on the vllm-api-key Secret and nothing else."
  }
  assert {
    condition     = kubernetes_role_binding_v1.vllm_api_key_read.subject[0].name == "prefect-worker" && kubernetes_role_binding_v1.vllm_api_key_read.subject[0].namespace == "prefect" && kubernetes_role_binding_v1.vllm_api_key_read.role_ref[0].name == kubernetes_role_v1.vllm_api_key_read.metadata[0].name
    error_message = "The api-key read Role must be bound to the worker's ServiceAccount."
  }

  # --- Worker environment: the topology the stack is applied with ---

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
    condition     = kubernetes_config_map_v1.bench_endpoint_topology.data["BENCH_ENDPOINT_INSTANCE_PROFILE"] == "slipstream-bench-host"
    error_message = "The worker must launch the bench host under the eks-owned instance profile."
  }
  assert {
    condition     = yamldecode(helm_release.prefect_worker.values[0]).worker.extraEnvVarsCMs == [kubernetes_config_map_v1.bench_endpoint_topology.metadata[0].name]
    error_message = "The worker must load the topology ConfigMap into its environment."
  }
}
