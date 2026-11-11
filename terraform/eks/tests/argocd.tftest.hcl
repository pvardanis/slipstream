# Plan-level tests for the Argo CD bootstrap on the eks stack (#509): the pinned Argo CD
# install, and the argocd-apps release that renders the two AppProjects and the root
# app-of-apps Application — the single handoff from Terraform to Argo CD. They run
# offline (aws, helm and kubernetes mocked), so they assert the install's shape: the
# chart pins, the root Application's cluster values and sync policy, and each project's
# scope. The root Application actually reporting Synced and Healthy is the cloud tier.
#
# The eks module builds IAM roles whose assume_role_policy is validated as JSON at
# plan; the mock provider's random string is not valid JSON, so every policy
# document gets a valid (empty) JSON default here. Mirrors karpenter.tftest.hcl.
mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "aws_partition" {
    defaults = {
      partition  = "aws"
      dns_suffix = "amazonaws.com"
    }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["eu-west-1a", "eu-west-1b", "eu-west-1c"] }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111122223333"
      arn        = "arn:aws:iam::111122223333:user/test"
    }
  }
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/test" }
  }
  # The worker layer plans with the stack; its image resolves to one content-sha tag.
  mock_data "aws_ecr_image" {
    defaults = { image_tags = ["main", "orchestrator-abc123"] }
  }
}

# The Karpenter chart is pulled through a second, us-east-1-pinned aws provider
# (public ECR's token API); mocking the default aws does not cover a distinct
# provider configuration, so without this the test makes a real STS call.
mock_provider "aws" {
  alias = "virginia"
}
mock_provider "helm" {}
mock_provider "kubernetes" {}

# The worker layer plans with the stack and reads the bootstrap remote state; stub it
# instead of reaching for real S3.
override_data {
  target = data.terraform_remote_state.bootstrap
  values = {
    outputs = {
      orchestration_image_repo_url    = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
      bench_image_repo_arn            = "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream-bench"
      bench_results_bucket_arn        = "arn:aws:s3:::slipstream-bench-results-abc123"
      sweep_dispatch_role_arn         = "arn:aws:iam::111122223333:role/slipstream-sweep-dispatch"
      grafana_cloud_write_secret_name = "grafana-cloud/write"
      cluster_name                    = "slipstream"
      prefect_namespace               = "prefect"
    }
  }
}

# The metrics stack (monitoring.tf) reads the grafana stack's remote_write outputs; stub
# them instead of reaching for real S3.
override_data {
  target = data.terraform_remote_state.grafana
  values = {
    outputs = {
      prometheus_remote_write_url      = "https://prometheus-prod-24-prod-eu-west-2.grafana.net/api/prom/push"
      prometheus_remote_write_username = "1234567"
      tempo_otlp_endpoint              = "tempo-prod-10-prod-eu-west-2.grafana.net:443"
      tempo_username                   = "7654321"
    }
  }
}

variables {
  state_bucket = "slipstream-tfstate-abc123"
}

run "argocd_install_is_pinned" {
  command = plan

  # The test repeats the pins as literals, so bumping a chart default in variables.tf is
  # a deliberate change that has to touch this test too.
  assert {
    condition     = helm_release.argocd.repository == "https://argoproj.github.io/argo-helm"
    error_message = "Argo CD must install from the upstream argo-helm repository."
  }
  assert {
    condition     = helm_release.argocd.chart == "argo-cd"
    error_message = "The Argo CD release must deploy the argo-cd chart."
  }
  assert {
    condition     = helm_release.argocd.version == "10.17.2"
    error_message = "The argo-cd chart must be pinned to 10.17.2."
  }
  assert {
    condition     = helm_release.argocd.namespace == "argocd"
    error_message = "Argo CD must install into the argocd namespace."
  }

  # A broken install (crashlooping controller, bad CRDs) fails cluster-up loudly and
  # rolls back, rather than leaving a half-up Argo CD the root Application lands on.
  assert {
    condition     = helm_release.argocd.wait == true && helm_release.argocd.atomic == true
    error_message = "The Argo CD release must wait and be atomic so a broken install fails the apply."
  }

  # No SSO is configured (the UI is reached by port-forward with the admin login), so
  # Dex would be an idle pod on the CPU node.
  assert {
    condition     = yamldecode(helm_release.argocd.values[0]).dex.enabled == false
    error_message = "Dex must be disabled: no SSO is configured."
  }

  # The CRDs come off with the release, so cluster-down's uninstall leaves nothing
  # behind and reports no kept resources.
  assert {
    condition     = yamldecode(helm_release.argocd.values[0]).crds.keep == false
    error_message = "The Argo CD CRDs must not be kept on uninstall."
  }
}

run "root_application_carries_cluster_values" {
  command = plan

  # The argocd-apps chart renders the AppProjects and the root Application as a Helm
  # release ordered after the Argo CD install, so their CRDs exist before they apply.
  assert {
    condition     = helm_release.argocd_apps.repository == "https://argoproj.github.io/argo-helm"
    error_message = "The root Application must install from the upstream argo-helm repository."
  }
  assert {
    condition     = helm_release.argocd_apps.chart == "argocd-apps"
    error_message = "The root Application release must deploy the argocd-apps chart."
  }
  assert {
    condition     = helm_release.argocd_apps.version == "2.0.8"
    error_message = "The argocd-apps chart must be pinned to 2.0.8."
  }
  assert {
    condition     = helm_release.argocd_apps.namespace == "argocd"
    error_message = "The root Application must live in Argo CD's own namespace."
  }

  # A rejected AppProject or Application fails cluster-up and rolls the release back,
  # rather than leaving the projects without the root Application.
  assert {
    condition     = helm_release.argocd_apps.atomic == true
    error_message = "The root Application release must be atomic so a rejected apply rolls back."
  }

  # The root Application syncs the app-of-apps chart on main from this repo, in its own
  # project, writing child Applications into Argo CD's namespace.
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.project == "root"
    error_message = "The root Application must belong to the root project."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.repoURL == "https://github.com/pvardanis/slipstream.git"
    error_message = "The root Application must sync from the slipstream repository."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.path == "k8s/argocd/apps"
    error_message = "The root Application must sync the app-of-apps chart at k8s/argocd/apps."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.targetRevision == "main"
    error_message = "The root Application must track main by default."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.destination == { server = "https://kubernetes.default.svc", namespace = "argocd" }
    error_message = "The root Application must write its child Applications into the in-cluster argocd namespace."
  }

  # The cluster values Terraform knows, handed to the app-of-apps chart: the repository
  # and revision its child apps sync from (the root's own), the orchestration image
  # repository (bootstrap ECR) and the content-sha tag resolved from its :main image, the
  # Prefect server's namespace and in-cluster API URL, the worker's Pod Identity-bound
  # ServiceAccount, and the bench endpoint topology ConfigMap name the worker reads.
  assert {
    condition = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.source == {
      repoURL        = "https://github.com/pvardanis/slipstream.git"
      targetRevision = "main"
    }
    error_message = "The root Application must hand its child apps the repository and revision it syncs from."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.orchestrationImageRepository == "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
    error_message = "The root Application must carry the orchestration image repository URL from the bootstrap stack."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.orchestrationImageTag == "orchestrator-abc123"
    error_message = "The root Application must carry the content-sha tag resolved from the orchestration :main image."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.namespaces == { prefect = "prefect" }
    error_message = "The root Application must carry the prefect namespace the Prefect server runs in."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.prefectApiUrl == "http://prefect-server.prefect.svc.cluster.local:4200/api"
    error_message = "The root Application must carry the Prefect server's in-cluster API URL."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.serviceAccounts == { prefectWorker = "prefect-worker" }
    error_message = "The root Application must carry the worker ServiceAccount name Pod Identity binds."
  }
  assert {
    condition = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.configMaps == {
      benchEndpointTopology = "bench-endpoint-topology"
    }
    error_message = "The root Application must carry the bench-endpoint topology ConfigMap name."
  }

  # The root applies its child Application objects server-side. Sync options are per
  # Application, so this covers the root's own sync only.
  assert {
    condition     = contains(yamldecode(helm_release.argocd_apps.values[0]).applications.root.syncPolicy.syncOptions, "ServerSideApply=true")
    error_message = "The root Application must sync with ServerSideApply=true."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.syncPolicy.automated == { prune = true, selfHeal = true, allowEmpty = true }
    error_message = "The root Application must sync automatically, pruning removed apps (down to none) and reverting drift."
  }

  # Helm ignores a value the chart never reads, so a key renamed on one side only would
  # hand every child app an empty string. The keys Terraform passes must be the keys the
  # app-of-apps chart declares.
  assert {
    condition = (
      keys(yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject) == keys(yamldecode(file("${path.module}/../../k8s/argocd/apps/values.yaml")))
      && keys(yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.serviceAccounts) == keys(yamldecode(file("${path.module}/../../k8s/argocd/apps/values.yaml")).serviceAccounts)
      && keys(yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.configMaps) == keys(yamldecode(file("${path.module}/../../k8s/argocd/apps/values.yaml")).configMaps)
      && keys(yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.namespaces) == keys(yamldecode(file("${path.module}/../../k8s/argocd/apps/values.yaml")).namespaces)
      && keys(yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.source) == keys(yamldecode(file("${path.module}/../../k8s/argocd/apps/values.yaml")).source)
    )
    error_message = "The root Application's values must have exactly the keys k8s/argocd/apps/values.yaml declares."
  }
}

# The tracked revision is a knob so the live bootstrap can be verified from a branch
# before its app-of-apps chart reaches main.
run "root_application_revision_is_configurable" {
  command = plan

  variables {
    argocd_target_revision = "feat/argocd-bootstrap"
  }

  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.targetRevision == "feat/argocd-bootstrap"
    error_message = "The root Application's revision must come from var.argocd_target_revision."
  }
  # Its child apps sync from the same revision, so a branch verifies them too.
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.source.targetRevision == "feat/argocd-bootstrap"
    error_message = "The child apps' revision must be the root Application's."
  }
}

run "app_projects_are_scoped" {
  command = plan

  # The root project may only write Application objects into Argo CD's namespace. A
  # wider grant there would reach Argo CD's own ConfigMaps and Secrets (argocd-cm,
  # argocd-rbac-cm, argocd-secret), letting a commit to the app-of-apps chart rewrite
  # Argo CD itself.
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).projects.root.sourceRepos == ["https://github.com/pvardanis/slipstream.git"]
    error_message = "The root project must source only from the slipstream repository."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).projects.root.destinations == [{ server = "https://kubernetes.default.svc", namespace = "argocd" }]
    error_message = "The root project must deploy only into the in-cluster argocd namespace."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).projects.root.namespaceResourceWhitelist == [{ group = "argoproj.io", kind = "Application" }]
    error_message = "The root project must manage only Argo CD Application objects."
  }

  # The workloads project deploys from this repo and the upstream prefect-helm chart
  # repository (the worker's chart) into the namespaces the application workloads run
  # in, and nowhere else.
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).projects.workloads.sourceRepos == ["https://github.com/pvardanis/slipstream.git", "https://prefecthq.github.io/prefect-helm"]
    error_message = "The workloads project must source only from the slipstream repository and the prefect-helm chart repository."
  }
  # The worker's chart (pinned in its Application, which Argo CD syncs from git) and the
  # server's (var.prefect_server_chart_version) are one prefect-helm chart version, so the
  # worker speaks the server's Prefect version.
  assert {
    condition     = regex("targetRevision: (\\S+)", file("${path.module}/../../k8s/argocd/apps/templates/prefect-worker.yaml"))[0] == var.prefect_server_chart_version
    error_message = "The prefect-worker Application's chart version must equal var.prefect_server_chart_version."
  }
  assert {
    condition = yamldecode(helm_release.argocd_apps.values[0]).projects.workloads.destinations == [
      { server = "https://kubernetes.default.svc", namespace = "prefect" },
      { server = "https://kubernetes.default.svc", namespace = "slipstream" },
    ]
    error_message = "The workloads project must deploy only into the in-cluster prefect and slipstream namespaces."
  }

  # Argo CD denies every cluster-scoped kind a project does not whitelist, so neither
  # project may carry a cluster resource whitelist: no app in them creates a namespace,
  # CRD or ClusterRole.
  assert {
    condition = alltrue([
      for name, project in yamldecode(helm_release.argocd_apps.values[0]).projects :
      !contains(keys(project), "clusterResourceWhitelist")
    ])
    error_message = "No AppProject may whitelist cluster-scoped resources."
  }
  assert {
    condition     = keys(yamldecode(helm_release.argocd_apps.values[0]).projects) == ["root", "workloads"]
    error_message = "Terraform must hand Argo CD exactly the root and workloads projects."
  }
}
