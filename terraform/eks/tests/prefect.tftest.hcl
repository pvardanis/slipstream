# Plan-level tests for the self-hosted Prefect server layer on the eks stack: the
# EBS CSI driver's IAM role, the default gp3 StorageClass, and the prefect-server
# Helm release with SQLite on an EBS PVC (ADR-0015 amendment, #185). They run
# offline — the aws, helm, and kubernetes providers are mocked — so the assertions
# check the install's shape (managed policy wired to the CSI role, the storage
# class's provisioner/binding/reclaim, the release's SQLite-not-Postgres values and
# single-attach Recreate strategy) without standing a cluster up. A PVC actually
# binding and surviving a pod bounce is the cloud tier (#92 / manual verify).
#
# Two joins are outside this tier's reach: the aws-ebs-csi-driver addon's
# pod_identity_association (main.tf) binds this role to the driver, but it is a
# module.eks *input*, which terraform test cannot assert; and the Pod Identity trust
# policy is mock-defaulted to empty JSON below. Both are covered only at the cloud
# tier (#92) — the role's existence and managed policy are all the plan can guard.

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
}

# The Karpenter chart is pulled through a second, us-east-1-pinned aws provider
# (public ECR's token API); mocking the default aws does not cover a distinct
# provider configuration, so without this the test makes a real STS call.
mock_provider "aws" {
  alias = "virginia"
}
mock_provider "helm" {}
mock_provider "kubernetes" {}

run "prefect_server_layer_shape" {
  command = plan

  # The EBS CSI controller authenticates through a Pod Identity association to this
  # role; the AWS-managed EBS CSI policy is the permission that lets it create and
  # delete the volume backing the PVC. Guard the managed ARN so a rename cannot
  # silently strip the driver's IAM and wedge PVC provisioning.
  assert {
    condition     = aws_iam_role_policy_attachment.ebs_csi.policy_arn == "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
    error_message = "The EBS CSI role must attach the AWS-managed AmazonEBSCSIDriverPolicy."
  }

  # The gp3 StorageClass is the cluster's only dynamic provisioner (#185) and its
  # default, so an unqualified PVC — the one the Prefect chart creates — binds to it.
  assert {
    condition     = kubernetes_storage_class_v1.gp3.metadata[0].annotations["storageclass.kubernetes.io/is-default-class"] == "true"
    error_message = "gp3 must be the default StorageClass so the Prefect PVC binds without naming one."
  }
  assert {
    condition     = kubernetes_storage_class_v1.gp3.storage_provisioner == "ebs.csi.aws.com"
    error_message = "The StorageClass must provision through the EBS CSI driver."
  }
  assert {
    condition     = kubernetes_storage_class_v1.gp3.parameters["type"] == "gp3"
    error_message = "The StorageClass must provision gp3 volumes."
  }

  # Zonal EBS: WaitForFirstConsumer defers provisioning until the server pod is
  # scheduled, so the volume lands in the pod's AZ. Immediate binding can strand a
  # volume in another AZ and leave the pod Pending forever.
  assert {
    condition     = kubernetes_storage_class_v1.gp3.volume_binding_mode == "WaitForFirstConsumer"
    error_message = "The StorageClass must use WaitForFirstConsumer so the EBS volume lands in the scheduled pod's AZ."
  }

  # Reclaim Delete makes the volume die with its PVC: cluster-down destroys the Helm
  # release (uninstall deletes the PVC), the CSI driver then deletes the EBS volume,
  # so no volume outlives the cluster (ADR-0015 amendment: state dies with the cluster).
  assert {
    condition     = kubernetes_storage_class_v1.gp3.reclaim_policy == "Delete"
    error_message = "The StorageClass reclaim policy must be Delete so the EBS volume dies with the PVC on teardown."
  }

  # Money-safety backstop (ADR-0002): a volume that somehow orphans still carries the
  # Project tag the zero-leak sweep filters on, so it is caught, not silently billed.
  assert {
    condition     = kubernetes_storage_class_v1.gp3.parameters["tagSpecification_1"] == "Project=slipstream"
    error_message = "Provisioned volumes must carry Project=slipstream so an orphan is caught by the zero-leak sweep."
  }

  # The server installs from the upstream Prefect Helm repo, pinned so an apply can't
  # float onto an untested chart.
  assert {
    condition     = helm_release.prefect_server.repository == "https://prefecthq.github.io/prefect-helm"
    error_message = "Prefect must install from the upstream prefect-helm repository."
  }
  assert {
    condition     = helm_release.prefect_server.chart == "prefect-server"
    error_message = "Helm release must deploy the prefect-server chart."
  }
  assert {
    condition     = helm_release.prefect_server.version == var.prefect_server_chart_version
    error_message = "Chart version must be pinned to var.prefect_server_chart_version."
  }

  # Apply waits for the server to come up and rolls back atomically on failure, so a
  # broken install fails the apply loudly instead of leaving a half-up server the
  # work pool step then can't reach.
  assert {
    condition     = helm_release.prefect_server.wait == true
    error_message = "Helm release must wait for the server Deployment so a broken install fails the apply."
  }
  assert {
    condition     = helm_release.prefect_server.atomic == true
    error_message = "Helm release must be atomic so a failed install rolls back instead of leaving a half-up server."
  }

  # Decode the release's own values attribute — what the resource actually ships, not
  # just the source local — so repointing values, dropping the yamlencode, or deleting
  # the line is caught too. It is [yamlencode(local)], known at plan. Flipping the
  # database backend, dropping the PVC binding, or restoring a rolling update fails here.
  assert {
    condition     = yamldecode(helm_release.prefect_server.values[0]).postgresql.enabled == false
    error_message = "Postgres must be disabled — the server runs on SQLite (ADR-0015 amendment)."
  }
  assert {
    condition     = yamldecode(helm_release.prefect_server.values[0]).sqlite.enabled == true
    error_message = "SQLite must be the enabled database backend."
  }
  assert {
    condition     = yamldecode(helm_release.prefect_server.values[0]).sqlite.persistence.enabled == true
    error_message = "SQLite must persist to a PVC so a server-pod bounce keeps the run in the UI."
  }
  assert {
    condition     = yamldecode(helm_release.prefect_server.values[0]).sqlite.persistence.storageClassName == kubernetes_storage_class_v1.gp3.metadata[0].name
    error_message = "The SQLite PVC must bind the gp3 StorageClass this stack creates."
  }

  # SQLite lives on a single-attach EBS volume; Recreate tears the old pod down
  # before the new one starts, so a rollout never schedules two pods contending for
  # the same volume (the chart default RollingUpdate would deadlock).
  assert {
    condition     = yamldecode(helm_release.prefect_server.values[0]).server.updateStrategy.type == "Recreate"
    error_message = "The server must use the Recreate strategy so a rollout does not contend for the single-attach EBS volume."
  }
  assert {
    condition     = yamldecode(helm_release.prefect_server.values[0]).server.replicaCount == 1
    error_message = "The server must run a single replica — one SQLite writer on one volume."
  }
}

# The PVC size is wired from a variable, not a hardcoded literal; overriding it must
# change the rendered value (run history is disposable, so the size is a knob).
run "sqlite_volume_size_is_configurable" {
  command = plan

  variables {
    prefect_sqlite_volume_size = "5Gi"
  }

  assert {
    condition     = yamldecode(helm_release.prefect_server.values[0]).sqlite.persistence.size == "5Gi"
    error_message = "The SQLite PVC size must come from var.prefect_sqlite_volume_size."
  }
}
