# Plan-level tests for the Karpenter control plane on the eks stack. They run
# offline: the aws and helm providers are mocked, so the assertions check the
# install's shape — the controller Helm release, its wiring to the module's
# interruption queue and cluster, and the karpenter.sh/discovery contract the
# node pool (#90) selects on — without standing a cluster up. The real end-to-end
# proof (a GPU node actually provisions) costs money and is the cloud tier (#92).

# The module builds IAM roles whose assume_role_policy is validated as JSON at
# plan; the mock provider's random string is not valid JSON, so every policy
# document gets a valid (empty) JSON default here. Nothing real is created.
mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  # Managed-policy ARNs are built from the partition; the mock's random partition
  # fails ARN validation, so pin the real one.
  mock_data "aws_partition" {
    defaults = {
      partition  = "aws"
      dns_suffix = "amazonaws.com"
    }
  }
  # The stack slices three AZs; the mock returns a shorter random list otherwise.
  mock_data "aws_availability_zones" {
    defaults = { names = ["eu-west-1a", "eu-west-1b", "eu-west-1c"] }
  }
  # The module feeds the caller ARN into aws_iam_session_context, which validates it.
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111122223333"
      arn        = "arn:aws:iam::111122223333:user/test"
    }
  }
  # The cluster-creator access entry takes its principal from the session issuer ARN.
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/test" }
  }
}
mock_provider "helm" {}

run "karpenter_install_shape" {
  command = plan

  # The controller ships as a Helm release from the upstream OCI registry, pinned
  # to a chart version so an apply can't float onto an untested Karpenter.
  assert {
    condition     = helm_release.karpenter.repository == "oci://public.ecr.aws/karpenter"
    error_message = "Karpenter must install from the upstream public ECR OCI registry."
  }
  assert {
    condition     = helm_release.karpenter.chart == "karpenter"
    error_message = "Helm release must deploy the karpenter chart."
  }
  assert {
    condition     = helm_release.karpenter.version == var.karpenter_chart_version
    error_message = "Chart version must be pinned to var.karpenter_chart_version."
  }
  assert {
    condition     = helm_release.karpenter.namespace == "kube-system"
    error_message = "Karpenter must run in kube-system alongside the other control-plane addons."
  }

  # The release is wired to this cluster and to the module's spot-interruption
  # queue; without the queue name threaded through, the controller never receives
  # the two-minute spot notice and can't drain the node.
  assert {
    condition     = local.karpenter_helm_settings.clusterName == module.eks.cluster_name
    error_message = "Helm values must point Karpenter at this cluster."
  }
  assert {
    condition     = local.karpenter_helm_settings.interruptionQueue == module.karpenter.queue_name
    error_message = "Helm values must wire the module's interruption queue into the controller."
  }

  # The discovery tag is the contract the EC2NodeClass subnet/security-group
  # selectors resolve against in #90: change the key or value and node provisioning
  # silently finds nothing. Lock it to karpenter.sh/discovery = <cluster name>.
  assert {
    condition     = local.karpenter_discovery_tags["karpenter.sh/discovery"] == var.cluster_name
    error_message = "Discovery tag must be karpenter.sh/discovery set to the cluster name."
  }
}
