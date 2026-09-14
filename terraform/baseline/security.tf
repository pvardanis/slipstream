# Network locks around the exposure. The load balancer admits only the operator
# CIDR on 443 (mTLS is the real gate; the CIDR is a cheap extra layer). The node
# group's security group — owned by the eks module — gets a standalone rule
# admitting the load balancer on the vLLM NodePort, so only the ALB, not the
# open VPC, can reach the port.

resource "aws_security_group" "alb" {
  name_prefix = "${local.name}-alb-"
  description = "Ingress to the baseline load balancer"
  vpc_id      = local.eks.vpc_id

  ingress {
    description = "Operator access to the mutual-TLS listener"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.operator_cidr]
  }

  egress {
    description = "Forward to the node group"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.tags
}

# Standalone rule on the eks-owned node security group: inline would mean editing
# the module. Admits only the load balancer's security group on the NodePort.
resource "aws_vpc_security_group_ingress_rule" "node_from_alb" {
  security_group_id            = local.eks.node_security_group_id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = var.vllm_nodeport
  to_port                      = var.vllm_nodeport
  description                  = "Baseline load balancer to vLLM NodePort"

  tags = local.tags
}
