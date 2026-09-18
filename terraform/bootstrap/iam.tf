# The service-linked role EC2 Spot requires before any spot instance can launch.
# The Karpenter GPU node pool (#90) requests spot capacity, and the first spot
# CreateFleet in an account tries to auto-create this role — which fails if the
# caller lacks iam:CreateServiceLinkedRole, leaving the GPU pod Pending forever.
# It lives in the bootstrap stack, not eks, because the role is account-global and
# shared by every spot instance in the account: a copy in the eks stack would be
# deleted on every `just down`, pulling it out from under any other spot user and
# racing to recreate it on the next `just up`. Created once here, it outlives
# cluster teardown like the state bucket beside it.
resource "aws_iam_service_linked_role" "spot" {
  aws_service_name = "spot.amazonaws.com"
}
