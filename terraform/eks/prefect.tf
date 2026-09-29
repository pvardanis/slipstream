# Self-hosted OSS Prefect 3 server, in-cluster, riding the cluster's own duty-cycle:
# it comes up with `cluster-up` (this Helm release) and is torn down by `cluster-down`
# (ADR-0015 amendment 2026-09-29). SQLite on an EBS PVC so a server-pod bounce keeps
# the run in the UI; run history is disposable — S3 is the source of truth (ADR-0012).
# No Postgres, no Redis: a single-instance ephemeral server needs neither. The UI/API
# is reached over `kubectl port-forward` (just prefect-ui); no public endpoint, no ALB.

locals {
  prefect_namespace = "prefect"

  # Helm values as a map so the test can read the wiring back; encoded to YAML for the
  # release below. Postgres off / SQLite on is the database-backend choice; the PVC
  # binds the gp3 class this stack creates; Recreate + one replica keep a single
  # SQLite writer on the single-attach EBS volume across a rollout.
  prefect_server_helm_values = {
    postgresql = { enabled = false }

    sqlite = {
      enabled = true
      persistence = {
        enabled          = true
        size             = var.prefect_sqlite_volume_size
        storageClassName = kubernetes_storage_class_v1.gp3.metadata[0].name
      }
    }

    server = {
      replicaCount = 1
      # SQLite lives on a single-attach EBS volume. Recreate tears the old pod down
      # before the new one starts, so a rollout never schedules two pods contending
      # for the volume — the chart's default RollingUpdate would deadlock there.
      updateStrategy = { type = "Recreate" }
    }
  }
}

resource "helm_release" "prefect_server" {
  namespace        = local.prefect_namespace
  create_namespace = true
  name             = "prefect-server"
  repository       = "https://prefecthq.github.io/prefect-helm"
  chart            = "prefect-server"
  version          = var.prefect_server_chart_version

  # Block apply until the server Deployment is Ready and roll back atomically on
  # failure, so a broken install (bad image, unbound PVC, crashloop) fails the apply
  # loudly rather than leaving a half-up server the work-pool step can't reach.
  wait   = true
  atomic = true

  values = [yamlencode(local.prefect_server_helm_values)]

  # The chart's PVC binds the gp3 class (implicit dependency via the values above);
  # the explicit dependency on the eks module orders teardown so the release's
  # uninstall — and its PVC delete — starts before the EBS CSI addon is removed. The
  # driver reaps the backing volume asynchronously, so the ordering gives the driver
  # the chance to delete it while still installed, not a hard guarantee it finishes
  # first; reclaim=Delete + the Project tag + the zero-leak sweep are the backstop.
  depends_on = [module.eks]
}
