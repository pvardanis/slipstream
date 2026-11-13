# The Prefect worker's cold-start rights (ADR-0030): the cold-start-breakdown flow waits
# until no GPU NodeClaim remains before it scales the replica up, then reads the hosting
# NodeClaim's Launched and Initialized conditions. NodeClaims and nodes are cluster-scoped,
# so this is the worker's first ClusterRole; it is read-only (get, list, watch) on those
# two resources and nothing else. The flow's list on events sits in the namespaced
# vllm-gpu-manage Role (worker.tf), and scaling the replica uses that Role's patch on the
# Deployment.
resource "kubernetes_cluster_role_v1" "cold_start_read" {
  metadata {
    name = "cold-start-read"
  }

  rule {
    api_groups = ["karpenter.sh"]
    resources  = ["nodeclaims"]
    verbs      = ["get", "list", "watch"]
  }
  rule {
    api_groups = [""]
    resources  = ["nodes"]
    verbs      = ["get", "list", "watch"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "cold_start_read" {
  metadata {
    name = "prefect-worker-cold-start-read"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.cold_start_read.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = local.worker_service_account
    namespace = local.prefect_namespace
  }
}
