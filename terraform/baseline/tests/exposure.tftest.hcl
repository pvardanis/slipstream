# Plan-level tests for the baseline exposure stack. They run offline: the aws
# provider is mocked and the eks remote-state data source is overridden, so the
# assertions check the stack's shape and its security invariants without
# standing anything up. The real end-to-end proof is `just baseline-up` plus a
# client-cert curl, which costs money and is run by hand.

mock_provider "aws" {}
mock_provider "tls" {}

variables {
  state_bucket  = "slipstream-tf-state-test"
  operator_cidr = "203.0.113.7/32"
  vllm_api_key  = "test-key"
  vllm_nodeport = 30800
}

run "exposure_invariants" {
  command = plan

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

  # The bootstrap read feeds the bench host, not the exposure resources, but it is
  # part of the stack, so it must be overridden here too or the test reaches for
  # real remote state.
  override_data {
    target = data.terraform_remote_state.bootstrap
    values = {
      outputs = {
        bench_image_repo_url = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream-bench"
        bench_image_repo_arn = "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream-bench"
      }
    }
  }

  # The load balancer is public and an ALB (only the ALB terminates mutual TLS).
  assert {
    condition     = aws_lb.baseline.internal == false
    error_message = "Baseline load balancer must be internet-facing to measure from an external vantage."
  }
  assert {
    condition     = aws_lb.baseline.load_balancer_type == "application"
    error_message = "Must be an ALB: only the application load balancer terminates mutual TLS."
  }

  # The HTTPS listener terminates mutual TLS in verify mode against a trust store.
  assert {
    condition     = aws_lb_listener.https.port == 443
    error_message = "Listener must serve HTTPS on 443."
  }
  assert {
    condition     = aws_lb_listener.https.protocol == "HTTPS"
    error_message = "Listener protocol must be HTTPS so the ALB terminates TLS."
  }
  assert {
    condition     = one(aws_lb_listener.https.mutual_authentication).mode == "verify"
    error_message = "Listener must enforce mutual TLS in verify mode; passthrough does not authenticate the client."
  }
  assert {
    condition     = aws_lb_listener.https.ssl_policy == "ELBSecurityPolicy-TLS13-1-2-2021-06"
    error_message = "Listener must pin the TLS 1.3 policy so a weaker policy can't slip onto a public endpoint."
  }

  # The trust store points at the CA bundle object; verify mode is meaningless if
  # it verifies against the wrong (or empty) bundle.
  assert {
    condition     = aws_lb_trust_store.mtls.ca_certificates_bundle_s3_key == aws_s3_object.ca_bundle.key
    error_message = "Trust store must reference the uploaded CA bundle object."
  }

  # The target group forwards to the vLLM NodePort on the node group over plain
  # HTTP (TLS is already terminated at the listener) and health-checks /health.
  assert {
    condition     = aws_lb_target_group.vllm.port == var.vllm_nodeport
    error_message = "Target group must forward to the vLLM NodePort."
  }
  assert {
    condition     = aws_lb_target_group.vllm.protocol == "HTTP"
    error_message = "Target group must forward plain HTTP; the ALB already terminated TLS at the listener."
  }
  assert {
    condition     = one(aws_lb_target_group.vllm.health_check).path == "/health"
    error_message = "Target group must health-check /health, which vLLM leaves unauthenticated."
  }

  # The node group's instances are registered as targets via the autoscaling
  # attachment; without it the ALB has no backends and every request 503s.
  assert {
    condition     = aws_autoscaling_attachment.vllm["slipstream-cpu"].autoscaling_group_name == "slipstream-cpu"
    error_message = "The node autoscaling group must be attached to the target group."
  }

  # The ALB security group's rules are standalone rule resources (alb_operator
  # here, alb_from_host in host.tf), never inline blocks: mixing inline blocks
  # with standalone rules on one group makes the provider revoke the standalone
  # rules on every apply. The inline set is computed and unknown at plan, so the
  # "no inline rules" property is a structural guarantee (see security.tf); what
  # is asserted here is the standalone operator rule's shape.

  # The operator ingress rule admits exactly the operator CIDR on TCP 443. Exact
  # match on cidr_ipv4, so widening it (e.g. to 0.0.0.0/0) fails the test.
  assert {
    condition     = aws_vpc_security_group_ingress_rule.alb_operator.cidr_ipv4 == var.operator_cidr
    error_message = "ALB operator ingress rule must admit only the operator CIDR."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.alb_operator.from_port == 443 && aws_vpc_security_group_ingress_rule.alb_operator.to_port == 443 && aws_vpc_security_group_ingress_rule.alb_operator.ip_protocol == "tcp"
    error_message = "ALB operator ingress rule must be TCP 443 only."
  }

  # The node security group admits the NodePort on TCP only, on the eks-owned SG.
  # This is the other half of the network lock ("only the load balancer reaches
  # the port"); a regression widening the port or target SG must fail here.
  assert {
    condition     = aws_vpc_security_group_ingress_rule.node_from_alb.security_group_id == "sg-nodes"
    error_message = "NodePort ingress rule must be attached to the eks node security group."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.node_from_alb.from_port == var.vllm_nodeport && aws_vpc_security_group_ingress_rule.node_from_alb.to_port == var.vllm_nodeport && aws_vpc_security_group_ingress_rule.node_from_alb.ip_protocol == "tcp"
    error_message = "NodePort ingress rule must admit only TCP on the vLLM NodePort."
  }
}
