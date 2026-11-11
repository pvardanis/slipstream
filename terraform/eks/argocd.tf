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

  # The root app-of-apps Application: it syncs k8s/argocd/apps from this repo and writes
  # the child Applications that chart renders into Argo CD's namespace. Its Helm values
  # are the cluster-specific names only Terraform knows — the orchestration image
  # repository from the bootstrap stack, the ServiceAccount Pod Identity binds, and the
  # ConfigMaps this stack publishes — and they are the only Terraform-to-Argo CD handoff.
  # Referencing the ConfigMaps orders the root after them, so no child app syncs
  # before what it reads exists.
  argocd_root_application = {
    namespace = local.argocd_namespace
    project   = "root"
    source = {
      repoURL        = "https://github.com/pvardanis/slipstream.git"
      targetRevision = var.argocd_target_revision
      path           = "k8s/argocd/apps"
      helm = {
        valuesObject = {
          orchestrationImageRepository = local.orchestration_repo_url
          serviceAccounts = {
            prefectWorker = local.worker_service_account
          }
          configMaps = {
            benchEndpointTopology = kubernetes_config_map_v1.bench_endpoint_topology.metadata[0].name
            grafanaCloudEndpoints = kubernetes_config_map_v1.grafana_cloud_endpoints.metadata[0].name
          }
        }
      }
    }
    destination = {
      server    = "https://kubernetes.default.svc"
      namespace = local.argocd_namespace
    }
    # Automated with prune and self-heal, so main is what runs: a child app removed from
    # the chart is deleted and hand edits are reverted. Server-side apply means a
    # manifest that omits a field (an autoscaler-owned `replicas`) never resets it.
    syncPolicy = {
      automated   = { prune = true, selfHeal = true }
      syncOptions = ["ServerSideApply=true"]
    }
  }

  argocd_apps_helm_values = {
    applications = { root = local.argocd_root_application }
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

resource "helm_release" "argocd_apps" {
  namespace        = local.argocd_namespace
  create_namespace = false
  name             = "argocd-apps"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argocd-apps"
  version          = var.argocd_apps_chart_version

  values = [yamlencode(local.argocd_apps_helm_values)]

  # The Application and AppProject CRDs ship with the Argo CD chart, so this release
  # applies only once Argo CD is installed, and uninstalls before it at cluster-down.
  depends_on = [helm_release.argocd]
}
