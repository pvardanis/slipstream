# Kubernetes access for the sweep-dispatch workflow (#180): the CI trigger that fires a
# knob-sweep run from the GitHub UI (ADR-0015 'CI later' arm). The workflow assumes the
# bootstrap stack's sweep-dispatch role over OIDC, then reaches the in-cluster Prefect API
# over `kubectl port-forward svc/prefect-server` (no public endpoint, ADR-0015). An EKS
# access entry maps that role to a Kubernetes group, and a namespaced Role grants the group
# only what the port-forward needs in the prefect namespace — no AWS-managed access policy,
# no cluster-wide grant.

locals {
  sweep_dispatch_group = "sweep-dispatch"
}

resource "aws_eks_access_entry" "sweep_dispatch" {
  cluster_name  = module.eks.cluster_name
  principal_arn = data.terraform_remote_state.bootstrap.outputs.sweep_dispatch_role_arn
  type          = "STANDARD"

  # The group is the entry's only Kubernetes identity; the RoleBinding below is what gives
  # it any permission at all.
  kubernetes_groups = [local.sweep_dispatch_group]

  tags = local.tags
}

# What `kubectl port-forward svc/prefect-server` needs: read the Service to resolve its
# backing pod, read that pod, and open the forward on the pods/portforward subresource.
# kubectl opens the forward over WebSockets, an HTTP GET authorized as `get`, and the
# cluster additionally checks `create` on that upgrade
# (AuthorizePodWebsocketUpgradeCreatePermission); the SPDY path is `create` alone. So
# the subresource carries both verbs. Namespaced (not a ClusterRole), so the grant
# cannot reach beyond prefect.
resource "kubernetes_role_v1" "sweep_dispatch" {
  metadata {
    name      = "sweep-dispatch"
    namespace = local.prefect_namespace
  }

  rule {
    api_groups = [""]
    resources  = ["services", "pods"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods/portforward"]
    verbs      = ["get", "create"]
  }

  # The prefect namespace is created by the server's Helm release.
  depends_on = [helm_release.prefect_server]
}

resource "kubernetes_role_binding_v1" "sweep_dispatch" {
  metadata {
    name      = "sweep-dispatch"
    namespace = local.prefect_namespace
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.sweep_dispatch.metadata[0].name
  }

  subject {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Group"
    name      = local.sweep_dispatch_group
  }
}
