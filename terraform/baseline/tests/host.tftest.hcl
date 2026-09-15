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

# File-scoped overrides so every run (invariants and the validation-rejection
# runs) plans against mocked remote state instead of reaching for real S3.
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
    }
  }
}

run "bench_host_invariants" {
  # apply, not plan: several invariants check IAM policy documents and bucket
  # ARNs that are provider-computed and unknown until apply. The mock provider
  # fills them with generated values, so nothing real is created.
  command = apply

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
  assert {
    condition     = one(aws_instance.bench_host.metadata_options).http_endpoint == "enabled"
    error_message = "Bench host metadata endpoint must stay enabled so the boot script can read its instance id."
  }

  # Editing the boot script must replace the host, not just mutate the launch
  # config, or a running host silently keeps stale boot logic.
  assert {
    condition     = aws_instance.bench_host.user_data_replace_on_change == true
    error_message = "Bench host must set user_data_replace_on_change so a script change replaces the host."
  }

  # The registry host is the repo URL up to the first slash; a bad split would
  # send docker login to the wrong registry and fail every pull at boot.
  assert {
    condition     = local.ecr_registry == "111122223333.dkr.ecr.eu-west-1.amazonaws.com"
    error_message = "ecr_registry must be the repository URL's registry host (up to the first slash)."
  }

  # The boot script is rendered with the results bucket and image reference it
  # needs; a wrong value boots a healthy-looking host that cannot run the sweep.
  assert {
    condition     = strcontains(local.bench_user_data, aws_s3_bucket.results.id)
    error_message = "Rendered boot script must carry the results bucket name."
  }
  assert {
    condition     = strcontains(local.bench_user_data, local.bench_image_ref)
    error_message = "Rendered boot script must carry the bench image reference."
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
  # These assertions check the rule's shape only; the mock provider does not
  # reconcile security-group rules, so it cannot prove the rule survives an apply
  # (that it doesn't collide with inline rules on the ALB SG). That drift-safety
  # is a structural guarantee: the ALB SG is kept free of inline rules (security.tf).
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
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.bench_host_secrets.policy).Statement) == 1 && jsondecode(aws_iam_role_policy.bench_host_secrets.policy).Statement[0].Effect == "Allow"
    error_message = "Secrets policy must be a single Allow statement; a second statement could grant more."
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
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.bench_host_results.policy).Statement) == 1 && jsondecode(aws_iam_role_policy.bench_host_results.policy).Statement[0].Effect == "Allow"
    error_message = "Results policy must be a single Allow statement; a second statement could grant more."
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
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.bench_host_ecr_pull.policy).Statement) == 1 && jsondecode(aws_iam_role_policy.bench_host_ecr_pull.policy).Statement[0].Effect == "Allow"
    error_message = "ECR pull policy must be a single Allow statement; a second statement could grant more."
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
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.bench_host_ecr_auth.policy).Statement) == 1 && jsondecode(aws_iam_role_policy.bench_host_ecr_auth.policy).Statement[0].Effect == "Allow"
    error_message = "ECR auth policy must be a single Allow statement; a second statement could widen the wildcard grant."
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

# The host runs an L4 nginx stream proxy so the sweep can speak plain HTTP to
# localhost while mutual TLS to the ALB happens in the proxy. These assertions
# check the boot install and the rendered proxy config's shape offline; the real
# proof that the handshake completes is the hand-run sweep's smoke step.
run "bench_proxy_config" {
  command = apply

  # The boot script installs nginx and the stream module (not built into the base
  # nginx package on AL2023) and drops the rendered proxy config and up-script.
  assert {
    condition     = strcontains(local.bench_user_data, "nginx")
    error_message = "Boot script must install nginx for the mTLS proxy."
  }
  assert {
    condition     = strcontains(local.bench_user_data, "nginx-mod-stream")
    error_message = "Boot script must install the nginx stream module; the base nginx package omits it."
  }
  assert {
    condition     = strcontains(local.bench_user_data, local.bench_proxy_conf)
    error_message = "Boot script must write the rendered proxy config to the host."
  }
  assert {
    condition     = strcontains(local.bench_user_data, "bench-proxy-up.sh")
    error_message = "Boot script must install the proxy up-script the sweep drives."
  }

  # The proxy listens on loopback only: it must never be reachable off the host,
  # so the client cert never leaves as an open relay.
  assert {
    condition     = strcontains(local.bench_proxy_conf, "listen 127.0.0.1:${local.bench_proxy_port}")
    error_message = "Proxy must listen on loopback only."
  }
  # It is an L4 stream proxy, not an http{} proxy, so nothing buffers or re-chunks
  # the token stream the latency metrics are measured from.
  assert {
    condition     = strcontains(local.bench_proxy_conf, "stream {")
    error_message = "Proxy must be an L4 stream proxy so it does not buffer the token stream."
  }
  # It forwards to the ALB DNS name on 443, terminating mutual TLS upstream.
  assert {
    condition     = strcontains(local.bench_proxy_conf, "proxy_pass ${aws_lb.baseline.dns_name}:443")
    error_message = "Proxy must forward to the ALB DNS name on 443."
  }
  assert {
    condition     = strcontains(local.bench_proxy_conf, "proxy_ssl on")
    error_message = "Proxy must speak TLS upstream to the ALB."
  }

  # It presents the bench client's identity so the verify-mode listener admits it.
  assert {
    condition     = strcontains(local.bench_proxy_conf, "proxy_ssl_certificate ") && strcontains(local.bench_proxy_conf, "proxy_ssl_certificate_key ")
    error_message = "Proxy must present the bench client certificate and key."
  }
  # It verifies the ALB's server certificate against the CA, under the name the
  # cert was issued for (the ALB DNS name the self-signed cert cannot cover).
  assert {
    condition     = strcontains(local.bench_proxy_conf, "proxy_ssl_trusted_certificate ")
    error_message = "Proxy must verify the ALB certificate against the bench CA."
  }
  assert {
    condition     = can(regex("proxy_ssl_verify\\s+on", local.bench_proxy_conf))
    error_message = "Proxy must verify the ALB certificate (proxy_ssl_verify on)."
  }
  assert {
    condition     = can(regex("proxy_ssl_server_name\\s+on", local.bench_proxy_conf))
    error_message = "Proxy must send SNI so the ALB serves the matching certificate."
  }
  assert {
    condition     = strcontains(local.bench_proxy_conf, var.server_dns_name)
    error_message = "Proxy must verify against the issued server name (proxy_ssl_name)."
  }
  # TLS 1.3 is pinned upstream, matching the listener's ssl_policy on the ALB.
  assert {
    condition     = strcontains(local.bench_proxy_conf, "TLSv1.3")
    error_message = "Proxy must pin TLS 1.3 to the ALB."
  }
}

# A burstable instance type is rejected at plan: its CPU credits would add
# variance to the latency measurement the whole vantage exists to take.
run "rejects_burstable_instance_type" {
  command = plan

  variables {
    bench_host_instance_type = "t3.large"
  }

  expect_failures = [var.bench_host_instance_type]
}

# An empty image tag is rejected at plan rather than deferring an invalid image
# reference to pull time on the host.
run "rejects_empty_image_tag" {
  command = plan

  variables {
    bench_image_tag = ""
  }

  expect_failures = [var.bench_image_tag]
}
