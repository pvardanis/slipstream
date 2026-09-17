# Plan-level tests for the Karpenter control plane on the eks stack. They run
# offline: the aws and helm providers are mocked, so the assertions check the
# install's shape — the controller Helm release, its wiring to the module's
# interruption queue, cluster, and service account, and the karpenter.sh/discovery
# tag the node pool (#90) selects on — without standing a cluster up. The real
# end-to-end proof (a GPU node actually provisions) is the cloud tier (#92).

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

# The Karpenter chart is pulled through a second, us-east-1-pinned aws provider
# (public ECR's token API); it is a distinct provider configuration, so mocking
# the default aws above does not cover it — without this block the test makes a
# real STS call and fails with no credentials (i.e. in CI).
mock_provider "aws" {
  alias = "virginia"
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

  # Apply must wait for the controller to come up so a broken install fails loudly
  # rather than surfacing later as a GPU pod stuck Pending (see karpenter.tf).
  assert {
    condition     = helm_release.karpenter.wait == true
    error_message = "Helm release must wait for the controller Deployment so a broken install fails the apply."
  }

  # The values the release renders must wire this cluster, the module's
  # spot-interruption queue, and the service account the Pod Identity association
  # is bound to. The rendered values string is unknown until apply, so these read
  # the source map (local.karpenter_helm_values) the release yamlencodes: renaming
  # a settings key or dropping the service account then fails the test.
  assert {
    condition     = local.karpenter_helm_values.settings.clusterName == module.eks.cluster_name
    error_message = "Helm values must point Karpenter at this cluster."
  }
  assert {
    condition     = local.karpenter_helm_values.settings.interruptionQueue == module.karpenter.queue_name
    error_message = "Helm values must wire the module's interruption queue into the controller."
  }
  assert {
    condition     = local.karpenter_helm_values.serviceAccount.name == module.karpenter.service_account
    error_message = "Helm values must set the service account bound to the Pod Identity association, or the controller has no AWS permissions."
  }

  # The discovery tag is the contract the EC2NodeClass subnet/security-group
  # selectors resolve against in #90: change the key or value and node provisioning
  # silently finds nothing. Lock the key/value, and that the merge onto the private
  # subnets keeps the internal-elb role tag rather than clobbering it. (That the tag
  # lands on the live subnets/SG is proven in the cloud tier, #92.)
  assert {
    condition     = local.private_subnet_tags["karpenter.sh/discovery"] == var.cluster_name
    error_message = "Private subnets must carry karpenter.sh/discovery set to the cluster name."
  }
  assert {
    condition     = local.private_subnet_tags["kubernetes.io/role/internal-elb"] == 1
    error_message = "Merging the discovery tag must not drop the internal-elb role tag."
  }
  assert {
    condition     = local.karpenter_discovery_tags["karpenter.sh/discovery"] == var.cluster_name
    error_message = "Node security group must carry karpenter.sh/discovery set to the cluster name."
  }
}
