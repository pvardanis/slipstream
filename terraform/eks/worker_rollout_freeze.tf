# The Prefect worker's rollout-freeze rights (ADR-0029): a knob sweep holds Argo CD off
# the workloads for its run by adding a deny sync window to the workloads AppProject and
# removing it afterwards (slipstream_bench.orchestration.rollout_freeze). The worker reads
# the project and patches its spec.syncWindows, so the grant is get and patch on that one
# AppProject in Argo CD's namespace — nothing on the root project, Argo CD's ConfigMaps and
# Secrets, or the Applications themselves.
resource "kubernetes_role_v1" "rollout_freeze" {
  metadata {
    name      = "rollout-freeze"
    namespace = local.argocd_namespace
  }

  rule {
    api_groups     = ["argoproj.io"]
    resources      = ["appprojects"]
    verbs          = ["get", "patch"]
    resource_names = ["workloads"]
  }

  # The Argo CD release creates the namespace the Role lives in.
  depends_on = [helm_release.argocd]
}

resource "kubernetes_role_binding_v1" "rollout_freeze" {
  metadata {
    name      = "prefect-worker-rollout-freeze"
    namespace = local.argocd_namespace
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.rollout_freeze.metadata[0].name
  }

  # Cross-namespace subject: the worker's SA lives in prefect, the AppProject in argocd.
  subject {
    kind      = "ServiceAccount"
    name      = local.worker_service_account
    namespace = local.prefect_namespace
  }
}
