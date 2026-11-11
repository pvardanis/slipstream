# The deploy substrate's bootstrap on the eks stack (#509): Terraform installs Argo CD
# and hands it one root app-of-apps Application, the single seam between what Terraform
# provisions and what Argo CD deploys. Argo CD rides the cluster's duty-cycle like the
# rest of the stack — installed by `cluster-up`, removed by `cluster-down`.

locals {
  argocd_namespace = "argocd"
  # The repo Argo CD syncs from (public, so no repository credentials) and the cluster
  # it deploys into: the one it runs in.
  argocd_source_repo = "https://github.com/pvardanis/slipstream.git"
  argocd_in_cluster  = "https://kubernetes.default.svc"

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
      repoURL        = local.argocd_source_repo
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
      server    = local.argocd_in_cluster
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

  # Two projects, so the root Application's right to write Application objects into
  # Argo CD's namespace never extends to the workloads, nor theirs to Argo CD itself.
  # Neither whitelists a cluster-scoped kind, and Argo CD denies every cluster-scoped kind
  # a project does not whitelist: no app creates a namespace, CRD or ClusterRole.
  argocd_projects = {
    # The root project manages only Application objects in Argo CD's namespace. Any wider
    # grant there would reach Argo CD's own ConfigMaps and Secrets (argocd-cm,
    # argocd-rbac-cm), letting a commit to the app-of-apps chart rewrite Argo CD.
    root = {
      namespace   = local.argocd_namespace
      description = "The root app-of-apps Application: writes the child Applications."
      sourceRepos = [local.argocd_source_repo]
      destinations = [
        { server = local.argocd_in_cluster, namespace = local.argocd_namespace },
      ]
      namespaceResourceWhitelist = [{ group = "argoproj.io", kind = "Application" }]
    }
    # The application workloads' project, the one a rollout freeze's sync windows land
    # on: it deploys into the namespaces the worker and the OTel Collector run in.
    slipstream = {
      namespace   = local.argocd_namespace
      description = "The cluster's application workloads, deployed from main."
      sourceRepos = [local.argocd_source_repo]
      destinations = [
        { server = local.argocd_in_cluster, namespace = local.prefect_namespace },
        { server = local.argocd_in_cluster, namespace = kubernetes_namespace_v1.slipstream.metadata[0].name },
      ]
    }
  }

  argocd_apps_helm_values = {
    projects     = local.argocd_projects
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
