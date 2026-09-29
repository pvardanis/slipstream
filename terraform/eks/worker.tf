# The Prefect worker layer on the eks stack (#178): a process worker that polls the
# self-hosted server's work pool and runs a sweep as a subprocess in its own pod. Like
# the server (prefect.tf) it rides the cluster's duty-cycle — up with `cluster-up`,
# torn down by `cluster-down` — so no idle service outlives a campaign (ADR-0015
# amendment). The worker runs a lean orchestration image (Prefect + the sweep flow
# code, the `.[orchestration]` extra), NOT the Prefect-free bench-client image the host
# runs per cell (ADR-0012): it drives the sweep, issuing SSM SendCommand to the bench
# host and reconfiguring deploy/vllm-gpu, but never serves the model itself.
#
# Auth is keyless: a Pod Identity association grants the worker's ServiceAccount S3
# (results bucket) + SSM SendCommand (bench host), and an in-namespace Role scopes it
# to managing deploy/vllm-gpu — nothing wider (no cluster-admin, no static keys).

locals {
  # The image ref (repo:tag) doubles as the worker's on/off switch: it is empty until
  # the orchestration image ships in its own build stack, and an empty value leaves the
  # whole layer uncreated so `just cluster-up` stands the cluster up before that image
  # exists. Supplying the image is what brings the worker up.
  worker_enabled = var.prefect_worker_image != ""

  worker_service_account = "prefect-worker"

  # vllm-gpu runs in the slipstream namespace (k8s/vllm-gpu.yaml); the worker's RBAC is
  # scoped there. This stack creates the namespace so the Role/RoleBinding have somewhere
  # to land at cluster-up (the manifest's own `kubectl apply` of it stays idempotent).
  vllm_namespace  = "slipstream"
  vllm_deployment = "vllm-gpu"

  # ECR refs carry no port, so the single colon splits repo from tag.
  worker_image_parts      = split(":", var.prefect_worker_image)
  worker_image_repository = try(local.worker_image_parts[0], "")
  worker_image_tag        = try(local.worker_image_parts[1], "")

  # Helm values as a map so the test can read the wiring back; encoded to YAML for the
  # release below. A process worker (subprocess, not a per-run Job) matches the serial
  # single-GPU sweep; the chart creates the SA that Pod Identity binds, but this stack
  # owns the scoped RBAC, so the chart's own Role/RoleBinding (kubernetes-worker
  # permissions a process worker never uses) are off.
  prefect_worker_helm_values = {
    worker = {
      image = {
        repository = local.worker_image_repository
        prefectTag = local.worker_image_tag
      }
      config = {
        workPool = var.prefect_work_pool
        type     = "process"
      }
      apiConfig = "selfHostedServer"
      selfHostedServerApiConfig = {
        apiUrl = "http://prefect-server.${local.prefect_namespace}.svc.cluster.local:4200/api"
      }
      replicaCount = 1
    }
    serviceAccount = {
      create = true
      name   = local.worker_service_account
    }
    role        = { create = false }
    rolebinding = { create = false }
  }
}

# Scopes the SSM instance ARN to this account rather than a wildcard.
data "aws_caller_identity" "current" {}

# The worker assumes this role through the Pod Identity association below; the trust is
# the pods.eks.amazonaws.com principal with sts:TagSession alongside AssumeRole (the
# agent tags the session). jsonencode inline (not an aws_iam_policy_document data
# source) so the plan test can read the statements back — the mock provider blanks that
# data source to empty JSON.
resource "aws_iam_role" "prefect_worker" {
  count = local.worker_enabled ? 1 : 0
  name  = "${var.cluster_name}-prefect-worker"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })

  tags = local.tags
}

# Least-privilege permissions. S3 and SSM targets live in the bench-endpoint stack,
# which consumes this stack's outputs — referencing them back would cycle — and the
# bucket suffix and instance id are non-deterministic anyway, so both are scoped by
# ARN prefix / tag condition, not exact ARN.
resource "aws_iam_role_policy" "prefect_worker" {
  count = local.worker_enabled ? 1 : 0
  name  = "${var.cluster_name}-prefect-worker"
  role  = aws_iam_role.prefect_worker[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # The host writes results; the worker only reads them back (aws s3 sync
      # sweeps/…), so GetObject/ListBucket, never PutObject.
      {
        Sid      = "ResultsBucketObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.cluster_name}-bench-endpoint-results-*/*"
      },
      {
        Sid      = "ResultsBucketList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.cluster_name}-bench-endpoint-results-*"
      },
      # SendCommand needs the document and the instance both authorized. The document
      # carries no Project tag, so a single tag-conditioned statement would deny it;
      # the send is split so the document is unconditional and the instance target is
      # narrowed to Project=slipstream (the bench host), never a broad instance/*.
      {
        Sid      = "SsmSendCommandDocument"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = "arn:aws:ssm:${var.region}::document/AWS-RunShellScript"
      },
      {
        Sid       = "SsmSendCommandInstances"
        Effect    = "Allow"
        Action    = ["ssm:SendCommand"]
        Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = { StringEquals = { "ssm:resourceTag/Project" = "slipstream" } }
      },
      # Polling a command to completion (get-command-invocation) is not resource-scoped
      # in SSM, so it is granted on all invocations.
      {
        Sid      = "SsmCommandStatus"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation"]
        Resource = "*"
      },
    ]
  })
}

# Binds the worker's ServiceAccount (created by the chart, in the prefect namespace) to
# the role. A standalone resource, so — unlike the EBS CSI association, a module.eks
# input — the plan test can assert its namespace/SA wiring.
resource "aws_eks_pod_identity_association" "prefect_worker" {
  count           = local.worker_enabled ? 1 : 0
  cluster_name    = module.eks.cluster_name
  namespace       = local.prefect_namespace
  service_account = local.worker_service_account
  role_arn        = aws_iam_role.prefect_worker[0].arn
  tags            = local.tags
}

# The namespace vllm-gpu runs in, created here so the worker's Role/RoleBinding have a
# namespace to land in at cluster-up (they precede `just gpu-deploy`, which is where the
# manifest is otherwise first applied). `just gpu-deploy` re-applies this same namespace
# via `kubectl apply`, which stamps its own labels/annotations
# (last-applied-configuration) on it; ignore_changes there keeps the two owners from
# fighting over the metadata on every plan.
resource "kubernetes_namespace_v1" "slipstream" {
  count = local.worker_enabled ? 1 : 0
  metadata {
    name = local.vllm_namespace
  }

  lifecycle {
    ignore_changes = [metadata[0].labels, metadata[0].annotations]
  }
}

# The worker's RBAC, scoped to managing deploy/vllm-gpu and reading its rollout/logs —
# nothing wider. Namespaced (not a ClusterRole), so the grant can never reach beyond
# the slipstream namespace.
resource "kubernetes_role_v1" "vllm_gpu_manage" {
  count = local.worker_enabled ? 1 : 0
  metadata {
    name      = "vllm-gpu-manage"
    namespace = local.vllm_namespace
  }

  # get/patch narrowed to the one Deployment by name: read it and reconfigure it per
  # sweep point, and no other workload.
  rule {
    api_groups     = ["apps"]
    resources      = ["deployments"]
    verbs          = ["get", "patch"]
    resource_names = [local.vllm_deployment]
  }
  # Reading the rollout (kubectl rollout status) watches the Deployment; resourceNames
  # cannot restrict list/watch, but the namespace holds only vllm-gpu.
  rule {
    api_groups = ["apps"]
    resources  = ["deployments"]
    verbs      = ["list", "watch"]
  }
  # kubectl logs deploy/vllm-gpu resolves the Deployment's pods, then reads their logs.
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods/log"]
    verbs      = ["get"]
  }

  depends_on = [kubernetes_namespace_v1.slipstream]
}

resource "kubernetes_role_binding_v1" "vllm_gpu_manage" {
  count = local.worker_enabled ? 1 : 0
  metadata {
    name      = "prefect-worker-vllm-gpu-manage"
    namespace = local.vllm_namespace
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.vllm_gpu_manage[0].metadata[0].name
  }

  # Cross-namespace subject: the worker's SA lives in prefect, the grant applies in
  # slipstream.
  subject {
    kind      = "ServiceAccount"
    name      = local.worker_service_account
    namespace = local.prefect_namespace
  }
}

resource "helm_release" "prefect_worker" {
  count            = local.worker_enabled ? 1 : 0
  namespace        = local.prefect_namespace
  create_namespace = false
  name             = "prefect-worker"
  repository       = "https://prefecthq.github.io/prefect-helm"
  chart            = "prefect-worker"
  version          = var.prefect_worker_chart_version

  # Block apply until the worker Deployment is Ready and roll back atomically on
  # failure, so a broken install (bad image, unreachable server, crashloop) fails the
  # apply loudly rather than leaving a half-up worker.
  wait   = true
  atomic = true

  values = [yamlencode(local.prefect_worker_helm_values)]

  # The worker connects to the server on startup and creates the work pool if it is
  # absent, so it must come up after the server is Ready; the prefect namespace the
  # release deploys into is the one the server release created.
  depends_on = [helm_release.prefect_server]
}
