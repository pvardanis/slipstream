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

  # The target group forwards to the vLLM NodePort on the node group.
  assert {
    condition     = aws_lb_target_group.vllm.port == var.vllm_nodeport
    error_message = "Target group must forward to the vLLM NodePort."
  }

  # The load balancer's security group admits only the operator CIDR on 443.
  assert {
    condition     = alltrue([for r in aws_security_group.alb.ingress : contains(r.cidr_blocks, var.operator_cidr) && r.from_port == 443])
    error_message = "ALB security group must admit only the operator CIDR on 443."
  }
}
