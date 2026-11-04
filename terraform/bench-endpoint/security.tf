# Network locks around the exposure. The load balancer admits only the bench host's
# public IP on 443 (alb_from_host in host.tf); mTLS and the vLLM api-key are the
# gate. The node group's security group — owned by the eks module — gets a standalone
# rule admitting the load balancer on the vLLM NodePort, so only the ALB, not the
# open VPC, can reach the port.

# The ALB security group carries no inline rules: its rules are standalone rule
# resources (below, plus alb_from_host in host.tf). Mixing inline blocks with
# standalone rules on one security group makes the provider revoke the standalone
# rules on every apply, so the group is kept rule-free and all rules stand alone.
resource "aws_security_group" "alb" {
  name_prefix = "${local.name}-alb-"
  description = "Ingress to the bench endpoint load balancer"
  vpc_id      = var.vpc_id

  tags = local.tags
}

resource "aws_vpc_security_group_egress_rule" "alb_to_nodes" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "Forward to the node group"

  tags = local.tags
}

# Standalone rule on the eks-owned node security group: inline would mean editing
# the module. Admits only the load balancer's security group on the NodePort.
resource "aws_vpc_security_group_ingress_rule" "node_from_alb" {
  security_group_id            = var.node_security_group_id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = var.vllm_nodeport
  to_port                      = var.vllm_nodeport
  description                  = "Bench endpoint load balancer to vLLM NodePort"

  tags = local.tags
}
