# The deploy substrate's bootstrap on the eks stack (#509): Terraform installs Argo CD
# and hands it one root app-of-apps Application, the single seam between what Terraform
# provisions and what Argo CD deploys. Argo CD rides the cluster's duty-cycle like the
# rest of the stack — installed by `cluster-up`, removed by `cluster-down`.

locals {
  argocd_namespace = "argocd"
  # The repo Argo CD syncs from (public, so no repository credentials) and the cluster
  # it deploys into: the one it runs in.
  argocd_source_repo       = "https://github.com/pvardanis/slipstream.git"
  argocd_in_cluster_server = "https://kubernetes.default.svc"

  # Helm values as a map so the test can read the wiring back; encoded to YAML for the
  # release below. No SSO is configured — the UI is reached over `kubectl port-forward`
  # with the admin login — so Dex would be an idle pod on the CPU node. The CRDs are not
  # kept on uninstall: they come off with the release at cluster-down, taking any
  # remaining Applications and AppProjects with them.
  argocd_helm_values = {
    dex  = { enabled = false }
    crds = { keep = false }
  }

  # The root app-of-apps Application: it syncs k8s/argocd/apps from this repo and writes
  # the child Applications that chart renders into Argo CD's namespace. Its Helm values
  # are the repository and revision its child apps sync from (its own), and the
  # cluster-specific values only Terraform knows — the orchestration image repository
  # from the bootstrap stack, the Prefect server's namespace and API URL, the
  # ServiceAccount Pod Identity binds, and the bench endpoint topology ConfigMap the
  # worker reads — and they are the only Terraform-to-Argo CD handoff. Referencing that
  # ConfigMap orders the root after it; the Collector's grafana-cloud-endpoints ConfigMap
  # is ordered by the depends_on on helm_release.argocd_apps below.
  argocd_root_application = {
    namespace = local.argocd_namespace
    project   = "root"
    source = {
      repoURL        = local.argocd_source_repo
      targetRevision = var.argocd_target_revision
      path           = "k8s/argocd/apps"
      helm = {
        valuesObject = {
          source = {
            repoURL        = local.argocd_source_repo
            targetRevision = var.argocd_target_revision
          }
          orchestrationImageRepository = local.orchestration_repo_url
          prefectApiUrl                = local.prefect_server_api_url
          namespaces = {
            prefect = local.prefect_namespace
          }
          serviceAccounts = {
            prefectWorker = local.worker_service_account
          }
          configMaps = {
            benchEndpointTopology = kubernetes_config_map_v1.bench_endpoint_topology.metadata[0].name
          }
        }
      }
    }
    destination = {
      server    = local.argocd_in_cluster_server
      namespace = local.argocd_namespace
    }
    # Automated with prune and self-heal, so the tracked revision is what runs: a child
    # app removed from the chart is deleted (the last one too, by allowEmpty) and hand
    # edits are reverted. The root applies its child Application objects server-side;
    # sync options are per Application, so each child carries its own.
    syncPolicy = {
      automated   = { prune = true, selfHeal = true, allowEmpty = true }
      syncOptions = ["ServerSideApply=true", "RespectIgnoreDifferences=true"]
    }
    # Image Updater (image_updater.tf) writes the worker's image tag onto its child
    # Application as a Helm parameter, which the app-of-apps chart never sets. The root
    # ignores that field in its diff and, by RespectIgnoreDifferences, keeps the live value
    # when it syncs, so self-heal never reverts the tag.
    ignoreDifferences = [{
      group        = "argoproj.io"
      kind         = "Application"
      jsonPointers = ["/spec/source/helm/parameters"]
    }]
  }

  # Two projects scope what each layer may deploy: root writes only Application objects
  # into Argo CD's namespace, and workloads deploys only into the prefect and slipstream
  # namespaces. Neither whitelists a cluster-scoped kind, and Argo CD denies every
  # cluster-scoped kind a project does not whitelist. Argo CD's built-in default project
  # is left permissive, and a child Application names its own project, so these limits
  # hold for the child Applications that name workloads.
  argocd_projects = {
    # The root project manages only Application objects in Argo CD's namespace. Any wider
    # grant there would reach Argo CD's own ConfigMaps and Secrets (argocd-cm,
    # argocd-rbac-cm, argocd-secret), letting a commit to the app-of-apps chart rewrite
    # Argo CD.
    root = {
      namespace   = local.argocd_namespace
      description = "The root app-of-apps Application: writes the child Applications."
      sourceRepos = [local.argocd_source_repo]
      destinations = [
        { server = local.argocd_in_cluster_server, namespace = local.argocd_namespace },
      ]
      namespaceResourceWhitelist = [{ group = "argoproj.io", kind = "Application" }]
    }
    # The application workloads' project: it deploys into the namespaces the worker and
    # the OTel Collector run in, from this repo and the prefect-helm chart repository the
    # worker's chart comes from (the Prefect server's, too).
    workloads = {
      namespace   = local.argocd_namespace
      description = "The cluster's application workloads, deployed from this repo and the charts it pins."
      sourceRepos = [local.argocd_source_repo, helm_release.prefect_server.repository]
      destinations = [
        { server = local.argocd_in_cluster_server, namespace = local.prefect_namespace },
        { server = local.argocd_in_cluster_server, namespace = kubernetes_namespace_v1.slipstream.metadata[0].name },
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

  # Roll back atomically when the API server rejects an AppProject or the root
  # Application, so cluster-up fails without leaving the projects half-applied. Helm's
  # wait does not cover custom resources; cluster-up waits on the root Application's
  # sync and health itself.
  wait   = true
  atomic = true

  values = [yamlencode(local.argocd_apps_helm_values)]

  # The Application and AppProject CRDs ship with the Argo CD chart, so this release
  # applies only once Argo CD is installed, and uninstalls before it at cluster-down. The
  # worker connects to the Prefect server on startup and creates the work pool if it is
  # absent, so the root (and the worker app it writes) waits for the server to be Ready.
  # Pod Identity credentials are injected when a pod is admitted, and Argo CD never
  # restarts a pod that started without them, so the worker's association exists before
  # the root writes the worker app. The OTel Collector's app reads the
  # grafana-cloud-endpoints ConfigMap under its fixed name (k8s/otel-collector), so the
  # root applies only once that ConfigMap exists.
  depends_on = [
    helm_release.argocd,
    helm_release.prefect_server,
    aws_eks_pod_identity_association.prefect_worker,
    kubernetes_config_map_v1.grafana_cloud_endpoints,
  ]
}
