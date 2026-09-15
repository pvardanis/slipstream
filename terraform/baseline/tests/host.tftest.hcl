# Tests for the ephemeral bench host. Like exposure.tftest.hcl they run offline:
# the aws/tls providers are mocked and both remote-state reads (eks and bootstrap)
# are overridden, so the assertions check the host's shape and its security
# invariants — zero ingress, resource-scoped IAM, a locked-down results bucket —
# without launching an instance. The real end-to-end proof is a `just baseline-up`
# run driven from the host, which costs money and is run by hand.

# This suite applies (not just plans) so IAM policy documents and bucket ARNs are
# known when the invariants read them. Under apply the AWS provider validates ARN
# shape, and the mock provider's random strings are not valid ARNs, so every
# resource whose ARN feeds a validated field or an assertion gets a valid-ARN
# default here. Nothing real is created.
mock_provider "aws" {
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:eu-west-1:111122223333:loadbalancer/app/mock/0123456789abcdef" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:eu-west-1:111122223333:targetgroup/mock/0123456789abcdef" }
  }
  mock_resource "aws_lb_trust_store" {
    defaults = { arn = "arn:aws:elasticloadbalancing:eu-west-1:111122223333:truststore/mock/0123456789abcdef" }
  }
  mock_resource "aws_acm_certificate" {
    defaults = { arn = "arn:aws:acm:eu-west-1:111122223333:certificate/0123abcd-ef01-2345-6789-0123456789ab" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:eu-west-1:111122223333:secret:bench-client-mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::bench-mock-bucket" }
  }
  # A valid public IP so the host's "${public_ip}/32" renders a valid CIDR for the
  # ALB ingress rule; the mock's random string would not parse as an address.
  mock_resource "aws_instance" {
    defaults = { public_ip = "198.51.100.10" }
  }
}
mock_provider "tls" {}

variables {
  state_bucket  = "slipstream-tf-state-test"
  operator_cidr = "203.0.113.7/32"
  vllm_api_key  = "test-key"
  vllm_nodeport = 30800
}

run "bench_host_invariants" {
  # apply, not plan: several invariants check IAM policy documents and bucket
  # ARNs that are provider-computed and unknown until apply. The mock provider
  # fills them with generated values, so nothing real is created.
  command = apply

  override_data {
    target = data.terraform_remote_state.eks
    values = {
      outputs = {
        vpc_id                  = "vpc-test"
        public_subnets          = ["subnet-a", "subnet-b", "subnet-c"]
        node_security_group_id  = "sg-nodes"
        node_autoscaling_groups = ["slipstream-cpu"]
        region                  = "eu-west-1"
      }
    }
  }

  override_data {
    target = data.terraform_remote_state.bootstrap
    values = {
      outputs = {
        bench_image_repo_url = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream-bench"
        bench_image_repo_arn = "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream-bench"
        region               = "eu-west-1"
      }
    }
  }

  # The host launches into a public subnet with a public IP: it needs egress to
  # ECR, Secrets Manager, SSM, S3 and the public ALB, and holds no inbound role.
  assert {
    condition     = contains(local.eks.public_subnets, aws_instance.bench_host.subnet_id)
    error_message = "Bench host must launch in one of the eks public subnets."
  }
  assert {
    condition     = aws_instance.bench_host.associate_public_ip_address == true
    error_message = "Bench host must get a public IP so it can reach AWS APIs and the public ALB."
  }
  assert {
    condition     = aws_instance.bench_host.iam_instance_profile == aws_iam_instance_profile.bench_host.name
    error_message = "Bench host must run under the bench-host instance profile so it never carries static credentials."
  }

  # IMDSv2 required: a public host carrying role credentials must not expose the
  # token-less IMDSv1 endpoint, which is the classic SSRF path to those credentials.
  assert {
    condition     = one(aws_instance.bench_host.metadata_options).http_tokens == "required"
    error_message = "Bench host must require IMDSv2 (http_tokens = required)."
  }

  # The host security group admits nothing: access is via SSM (an outbound
  # session), not SSH, so there is no inbound attack surface at all.
  assert {
    condition     = length(aws_security_group.bench_host.ingress) == 0
    error_message = "Bench host security group must have zero ingress rules; access is via SSM, not inbound SSH."
  }

  # The ALB admits the host on 443 through its public /32: the host reaches the
  # internet-facing ALB by its public IP over the IGW hairpin, so a security-group
  # reference would not match. Scoped to the host's /32 on 443, nothing wider.
  assert {
    condition     = aws_vpc_security_group_ingress_rule.alb_from_host.security_group_id == aws_security_group.alb.id
    error_message = "Host-to-ALB ingress rule must be attached to the ALB security group."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.alb_from_host.cidr_ipv4 == "${aws_instance.bench_host.public_ip}/32"
    error_message = "ALB must admit only the bench host's public /32."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.alb_from_host.from_port == 443 && aws_vpc_security_group_ingress_rule.alb_from_host.to_port == 443 && aws_vpc_security_group_ingress_rule.alb_from_host.ip_protocol == "tcp"
    error_message = "Host-to-ALB ingress must be TCP 443 only."
  }

  # Egress is HTTPS only: ECR, Secrets Manager, SSM, S3 and the ALB listener all
  # speak 443. A wider egress rule would let the throwaway host talk anywhere.
  assert {
    condition     = length(aws_security_group.bench_host.egress) == 1
    error_message = "Bench host security group must have exactly one egress rule."
  }
  assert {
    condition     = one(aws_security_group.bench_host.egress).from_port == 443 && one(aws_security_group.bench_host.egress).to_port == 443 && one(aws_security_group.bench_host.egress).protocol == "tcp"
    error_message = "Bench host egress must be TCP 443 only."
  }

  # The instance role is assumable only by EC2: exactly one Allow statement whose
  # principal is the EC2 service. A second statement could widen who can assume it.
  assert {
    condition     = length(jsondecode(aws_iam_role.bench_host.assume_role_policy).Statement) == 1
    error_message = "Bench host assume-role policy must have exactly one statement."
  }
  assert {
    condition     = jsondecode(aws_iam_role.bench_host.assume_role_policy).Statement[0].Effect == "Allow" && jsondecode(aws_iam_role.bench_host.assume_role_policy).Statement[0].Principal.Service == "ec2.amazonaws.com"
    error_message = "Bench host role must be assumable only by the EC2 service."
  }

  # Secrets read is scoped to the one bench-client secret, never "*", and to the
  # read action only: the host reads its cert/key/CA/api-key and nothing else, and
  # must not be able to mutate or delete the secret.
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_secrets.policy).Statement[0].Resource == aws_secretsmanager_secret.bench_client.arn
    error_message = "Secrets read must be scoped to the bench-client secret ARN."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_secrets.policy).Statement[0].Action == "secretsmanager:GetSecretValue"
    error_message = "Secrets policy must grant only GetSecretValue, not a wider secretsmanager action."
  }

  # Results write is scoped to the results bucket's objects and to PutObject only:
  # the host writes measurement JSON, it does not read or delete run data.
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_results.policy).Statement[0].Resource == "${aws_s3_bucket.results.arn}/*"
    error_message = "Results write must be scoped to the results bucket objects."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_results.policy).Statement[0].Action == "s3:PutObject"
    error_message = "Results policy must grant only PutObject, not a wider s3 action."
  }

  # ECR image pull is scoped to the bench-client repository ARN, never "*", and to
  # the read/pull actions only: the host pulls the image, it must not push.
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_ecr_pull.policy).Statement[0].Resource == data.terraform_remote_state.bootstrap.outputs.bench_image_repo_arn
    error_message = "ECR pull must be scoped to the bench-client repository ARN."
  }
  assert {
    condition     = tolist(jsondecode(aws_iam_role_policy.bench_host_ecr_pull.policy).Statement[0].Action) == tolist(["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:BatchCheckLayerAvailability"])
    error_message = "ECR pull policy must grant only the layer/image read actions, not push."
  }

  # The one deliberately wildcard-scoped policy: ecr:GetAuthorizationToken is a
  # registry-wide action AWS won't let you scope to a repository. Pin it to that
  # single action so a regression can't widen the wildcard grant to, say, ecr:*.
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_ecr_auth.policy).Statement[0].Resource == "*"
    error_message = "ECR auth policy is registry-wide by necessity; resource must be exactly \"*\"."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_ecr_auth.policy).Statement[0].Action == "ecr:GetAuthorizationToken"
    error_message = "ECR auth policy must grant only GetAuthorizationToken on the registry-wide wildcard."
  }

  # SSM access is the AWS-managed core policy; it is what lets the operator open a
  # session to the host without any inbound rule.
  assert {
    condition     = aws_iam_role_policy_attachment.bench_host_ssm.policy_arn == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    error_message = "Bench host must attach the SSM managed instance core policy for keyless access."
  }

  # The results bucket blocks all public access; it holds measurement JSON, not
  # anything meant to be reachable from the internet.
  assert {
    condition     = aws_s3_bucket_public_access_block.results.block_public_acls && aws_s3_bucket_public_access_block.results.block_public_policy && aws_s3_bucket_public_access_block.results.ignore_public_acls && aws_s3_bucket_public_access_block.results.restrict_public_buckets
    error_message = "Results bucket must block all public access."
  }

  # The results bucket is force-destroyed so baseline-down removes it (and the
  # run's JSON) cleanly rather than failing on a non-empty bucket.
  assert {
    condition     = aws_s3_bucket.results.force_destroy == true
    error_message = "Results bucket must be force-destroyed so baseline-down can remove it."
  }
}
