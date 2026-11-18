# KEDA, the autoscaler that owns the serving fleet's replicas (ADR-0031): its
# operator, metrics server, admission webhooks and CRDs, installed as a cluster add-on
# (ADR-0029's split). The serving fleet's ScaledObject is Argo CD's; this release only
# makes the kind exist. Like the other add-ons it rides the cluster's duty-cycle: up with
# `cluster-up`, gone with `cluster-down`.

resource "helm_release" "keda" {
  namespace        = "keda"
  create_namespace = true
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = var.keda_chart_version

  # The chart templates its CRDs rather than shipping them under crds/, so Helm tracks
  # them: an upgrade updates their schemas and an uninstall removes them with the
  # release, taking every ScaledObject with them (the CRDs carry no
  # helm.sh/resource-policy: keep), which cluster-down's teardown of the whole cluster
  # does anyway. install is the chart's default, set here because the CRDs are what the
  # serving fleet's ScaledObject needs.
  values = [yamlencode({ crds = { install = true } })]

  # Block apply until the operator, metrics server and webhooks are Ready and roll back
  # atomically on failure, so a broken install fails cluster-up loudly rather than
  # leaving a ScaledObject to be rejected by a webhook that never came up.
  wait   = true
  atomic = true

  depends_on = [module.eks]
}
