# The serving fleet's namespace (ADR-0031): Argo CD syncs the fleet, its proxy and its
# ScaledObject into it (k8s/argocd/apps/templates/serving.yaml), and the fleet's pods
# read the api-key Secret created here. Terraform owns both, so the Secret exists before
# the app first syncs, and Argo CD, which does not delete a namespace it did not create,
# cannot take the Secret with it when the app is pruned.

resource "kubernetes_namespace_v1" "serving" {
  metadata {
    name = "serving"
  }
}

# The serving fleet's api-key, generated at plan and never stored: an ephemeral value
# goes only into the Secret's write-only data_wo, so it is in neither the plan nor the
# state. It is its own key, not the bench Engine's (just _ensure-api-key), which a
# kubectl recipe mints in slipstream outside Terraform.
ephemeral "random_password" "serving_api_key" {
  length  = 64
  special = false
}

resource "kubernetes_secret_v1" "serving_api_key" {
  metadata {
    name      = "vllm-api-key"
    namespace = kubernetes_namespace_v1.serving.metadata[0].name
  }

  # Terraform writes data_wo only when the revision changes, so the key stays stable
  # across applies; raising the revision rotates it.
  data_wo          = { api-key = ephemeral.random_password.serving_api_key.result }
  data_wo_revision = 1
}
