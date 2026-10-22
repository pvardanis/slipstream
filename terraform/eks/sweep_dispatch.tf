# Kubernetes access for the flow-dispatch workflow (#180) behind sweep-dispatch and
# kv-ab-dispatch: the CI triggers that fire a knob-sweep or KV cache A/B run from the
# GitHub UI (ADR-0015 'CI later' arm). The workflow assumes the bootstrap stack's
# sweep-dispatch role over OIDC, then reaches the in-cluster Prefect API over `kubectl
# port-forward svc/prefect-server` (no public endpoint, ADR-0015). An EKS access entry
# maps that role to a Kubernetes group, and a namespaced Role grants the group only what
# the port-forward needs in the prefect namespace — no AWS-managed access policy, no
# cluster-wide grant.

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

  # gh-vars sets EKS_CLUSTER_NAME from the bootstrap output, so the workflow targets the
  # cluster bootstrap names; fail the plan if that is not this one. try() turns a
  # bootstrap applied before the output existed into the same actionable failure.
  lifecycle {
    precondition {
      condition     = try(data.terraform_remote_state.bootstrap.outputs.cluster_name, null) == var.cluster_name
      error_message = "The bootstrap cluster_name output (${try(data.terraform_remote_state.bootstrap.outputs.cluster_name, "absent")}) must equal this stack's cluster_name (${var.cluster_name}), or the sweep-dispatch workflow targets another cluster. Set both to the same value and re-run `just bootstrap` then `just gh-vars`."
    }
  }
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

  # gh-vars sets PREFECT_NAMESPACE from the bootstrap output, so the workflow
  # port-forwards into the namespace bootstrap names; fail the plan if that is not this one.
  lifecycle {
    precondition {
      condition     = try(data.terraform_remote_state.bootstrap.outputs.prefect_namespace, null) == local.prefect_namespace
      error_message = "The bootstrap prefect_namespace output (${try(data.terraform_remote_state.bootstrap.outputs.prefect_namespace, "absent")}) must equal the namespace the Prefect server runs in (${local.prefect_namespace}), or the sweep-dispatch workflow port-forwards into nothing. Set it in terraform/bootstrap and re-run `just bootstrap` then `just gh-vars`."
    }
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
