# GPU utilisation per replica (ADR-0026, "Metrics" and "Panels: GPU"): NVIDIA's
# dcgm-exporter as one DaemonSet on the GPU nodes, with no GPU Operator (ADR-0006). It
# labels each GPU's series with the pod using it, read from the kubelet's pod-resources
# socket, and is scraped by the metrics stack's ServiceMonitor (monitoring.tf). With no
# GPU node up it runs no pod, and Karpenter ignores its pods when it finds a GPU node
# empty, so scale-to-zero still reaps the node.

locals {
  # Helm values as a map so the test can read the wiring back; encoded to YAML for the
  # release below.
  dcgm_exporter_helm_values = {
    # The label and taint the GPU NodePool puts on its nodes (k8s/gpu-node-pool.yaml).
    # The toleration list replaces the chart's default control-plane one, which EKS has
    # no nodes for.
    nodeSelector = { "nvidia.com/gpu" = "true" }
    tolerations  = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]

    # The chart's own ServiceMonitor carries no label the stack's Prometheus selects, and
    # its CRD comes from the stack's release; the stack scrapes the exporter instead.
    serviceMonitor = { enabled = false }

    # Memory limit equals the request so the exporter cannot use more of the g5's host
    # RAM than it reserves beside the vLLM replica; CPU is left unlimited. Helm merges
    # these over the chart's own values, so the chart's 200m CPU limit is removed by
    # setting it to null.
    resources = {
      requests = { cpu = "100m", memory = "512Mi" }
      limits   = { cpu = null, memory = "512Mi" }
    }
  }
}

resource "helm_release" "dcgm_exporter" {
  namespace  = kubernetes_namespace_v1.monitoring.metadata[0].name
  name       = "dcgm-exporter"
  repository = "https://nvidia.github.io/dcgm-exporter/helm-charts"
  chart      = "dcgm-exporter"
  version    = var.dcgm_exporter_chart_version

  # A DaemonSet with no GPU node up has no pod to wait for, so cluster-up does not wait
  # on a GPU. A chart or values that fail to render or apply still fail the apply and
  # roll back; a failure in the exporter pod itself shows only once a GPU node is up.
  wait   = true
  atomic = true

  values = [yamlencode(local.dcgm_exporter_helm_values)]

  depends_on = [module.eks]
}
