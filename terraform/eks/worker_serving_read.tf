# The Prefect worker's read on the serving fleet (ADR-0031): the serving fleet and the bench
# Engine share the GPU quota, so the bench flows refuse to start while the serving fleet's
# Deployment has replicas, and read its replica count to tell. A get on that one
# Deployment by name and nothing else in the serving namespace: the check reads that one
# object, so the Role grants no list or watch and the worker cannot see any other workload
# there. The worker's slipstream Role (worker.tf) is not widened.

locals {
  serving_deployment = "vllm-serving"
}

# The Role and its binding land in the serving namespace serving.tf creates.
resource "kubernetes_role_v1" "serving_fleet_read" {
  metadata {
    name      = "serving-fleet-read"
    namespace = kubernetes_namespace_v1.serving.metadata[0].name
  }

  rule {
    api_groups     = ["apps"]
    resources      = ["deployments"]
    verbs          = ["get"]
    resource_names = [local.serving_deployment]
  }
}

resource "kubernetes_role_binding_v1" "serving_fleet_read" {
  metadata {
    name      = "prefect-worker-serving-fleet-read"
    namespace = kubernetes_namespace_v1.serving.metadata[0].name
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.serving_fleet_read.metadata[0].name
  }

  # Cross-namespace subject: the worker's SA lives in prefect, the grant applies in serving.
  subject {
    kind      = "ServiceAccount"
    name      = local.worker_service_account
    namespace = local.prefect_namespace
  }
}
