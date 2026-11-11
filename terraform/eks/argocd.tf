# The deploy substrate's bootstrap on the eks stack (#509): Terraform installs Argo CD
# and hands it one root app-of-apps Application, the single seam between what Terraform
# provisions and what Argo CD deploys. Argo CD rides the cluster's duty-cycle like the
# rest of the stack — installed by `cluster-up`, removed by `cluster-down`.

locals {
  argocd_namespace = "argocd"

  # Helm values as a map so the test can read the wiring back; encoded to YAML for the
  # release below. No SSO is configured — the UI is reached over `kubectl port-forward`
  # with the admin login — so Dex would be an idle pod on the CPU node.
  argocd_helm_values = {
    dex = { enabled = false }
  }
}

resource "helm_release" "argocd" {
  namespace        = local.argocd_namespace
  create_namespace = true
  name             = "argocd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = var.argocd_chart_version

  # Block apply until the Argo CD workloads are Ready and roll back atomically on
  # failure, so a broken install fails cluster-up loudly rather than leaving a half-up
  # Argo CD for the root Application to land on.
  wait   = true
  atomic = true

  values = [yamlencode(local.argocd_helm_values)]

  # Installs once the cluster and its node group exist, and uninstalls before they are
  # torn down at cluster-down.
  depends_on = [module.eks]
}
