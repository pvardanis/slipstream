# The Prefect worker layer on the eks stack (#178): a process worker that polls the
# self-hosted server's work pool and runs a sweep as a subprocess in its own pod. Like
# the server (prefect.tf) it rides the cluster's duty-cycle — up with `cluster-up`,
# torn down by `cluster-down` — so no idle service outlives a campaign (ADR-0015
# amendment). The worker runs a lean orchestration image (Prefect + the sweep flow
# code, the `.[orchestration]` extra), NOT the Prefect-free bench-client image the host
# runs per cell (ADR-0012): it drives the sweep, issuing SSM SendCommand to the bench
# host and reconfiguring deploy/vllm-gpu, but never serves the model itself.
#
# Argo CD deploys the worker itself (k8s/argocd/apps/templates/prefect-worker.yaml); this
# stack resolves its image tag and hands it over through the root Application's values
# (argocd.tf), and keeps what the worker is bound to in AWS and the cluster.
#
# Auth is keyless: a Pod Identity association grants the worker's ServiceAccount S3
# (results bucket) + SSM SendCommand (bench host), and an in-namespace Role scopes it
# to managing deploy/vllm-gpu — nothing wider (no cluster-admin, no static keys). The
# scoped rights to apply and destroy the bench-endpoint stack live beside it in
# worker_bench_endpoint.tf.

locals {
  worker_service_account = "prefect-worker"

  # vllm-gpu runs in the slipstream namespace (k8s/vllm-gpu.yaml); the worker's RBAC is
  # scoped there. This stack creates the namespace so the Role/RoleBinding have somewhere
  # to land at cluster-up (the manifest's own `kubectl apply` of it stays idempotent).
  vllm_namespace  = "slipstream"
  vllm_deployment = "vllm-gpu"

  # The worker image: repository from the bootstrap ECR output, pinned to the immutable
  # content-sha tag the publish workflow puts on the :main manifest alongside :main (the
  # non-main tag on that manifest, resolved in data.tf). The worker's Application pins the
  # sha, so a re-apply re-resolves to a new immutable ref (a real rollout) and a cached
  # image on a node is always the right one under IfNotPresent. repository_name is the
  # path after the registry host, which is what the ECR data source looks the image up by.
  orchestration_repo_url  = data.terraform_remote_state.bootstrap.outputs.orchestration_image_repo_url
  orchestration_repo_name = trimprefix(local.orchestration_repo_url, "${split("/", local.orchestration_repo_url)[0]}/")
  # The bootstrap stack's bench-results bucket, the worker's S3 target. try() turns a
  # bootstrap applied before the bucket existed into the worker policy's precondition
  # failure rather than an unsupported-attribute error.
  bench_results_arn = try(data.terraform_remote_state.bootstrap.outputs.bench_results_bucket_arn, "")
  # The ECR data source's postcondition guarantees exactly one non-main tag, so the list
  # comprehension's [0] resolves it — and an empty list (a loosened postcondition or
  # drifted filter) fails the plan loudly instead of pinning "".
  worker_image_tag = [for t in data.aws_ecr_image.orchestration.image_tags : t if t != "main"][0]
}

# Scopes the SSM instance ARN to this account rather than a wildcard.
data "aws_caller_identity" "current" {}

# The worker assumes this role through the Pod Identity association below; the trust is
# the pods.eks.amazonaws.com principal with sts:TagSession alongside AssumeRole (the
# agent tags the session). jsonencode inline (not an aws_iam_policy_document data
# source) so the plan test can read the statements back — the mock provider blanks that
# data source to empty JSON.
resource "aws_iam_role" "prefect_worker" {
  name = "${var.cluster_name}-prefect-worker"

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

# Least-privilege permissions. The S3 target is the bootstrap stack's bench-results
# bucket, by its exact ARN. The SSM target is the bench host in the bench-endpoint stack,
# which consumes this stack's outputs — referencing it back would cycle — and its
# instance id is non-deterministic, so it is scoped by tag condition.
resource "aws_iam_role_policy" "prefect_worker" {
  name = "${var.cluster_name}-prefect-worker"
  role = aws_iam_role.prefect_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # The host writes the cell result objects; the worker reads them back (under
      # knob-sweep/… and kv-offload-ab/…), so GetObject/ListBucket across the whole bucket.
      {
        Sid      = "ResultsBucketObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${local.bench_results_arn}/*"
      },
      {
        Sid      = "ResultsBucketList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = local.bench_results_arn
      },
      # The worker persists its own resume state with persist_result=True: the task result
      # under prefect/results and the cache-key records under prefect/cache-keys. PutObject
      # is scoped to those two prefixes, never the host-owned cell objects under the
      # experiment roots.
      {
        Sid    = "PrefectStateObjects"
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "${local.bench_results_arn}/prefect/results/*",
          "${local.bench_results_arn}/prefect/cache-keys/*",
        ]
      },
      # The terminal render tasks upload each plot's PNG under knob-sweep/<run>/charts/ or
      # kv-offload-ab/<run>/charts/ as the run's durable copy (ADR-0018). PutObject is
      # scoped to exactly those charts prefixes, never the sibling cell-result JSONs the
      # host writes beside them.
      {
        Sid    = "ChartObjects"
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "${local.bench_results_arn}/knob-sweep/*/charts/*",
          "${local.bench_results_arn}/kv-offload-ab/*/charts/*",
        ]
      },
      # The kv-cache-extension-ab flow labels each rung with its arm, run, and prefix-cache
      # window, written as a <cell>.kv_ab.json object beside the host's raw <cell>.json
      # under kv-offload-ab/<run>/<arm>-r<n>/ (#326). PutObject is scoped to that suffix,
      # never the host's raw results it sits beside.
      {
        Sid      = "KvAbLabelObjects"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${local.bench_results_arn}/kv-offload-ab/*.kv_ab.json"
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

  lifecycle {
    precondition {
      condition     = local.bench_results_arn != ""
      error_message = "The bootstrap stack has no bench_results_bucket_arn output, so the Prefect worker has no bench-results bucket to read and write. Run `just bootstrap` to create the bucket, then re-run this plan."
    }
  }
}

# Binds the worker's ServiceAccount (created by the worker's chart, which Argo CD syncs
# into the prefect namespace) to the role. A standalone resource, so — unlike the EBS CSI association, a module.eks
# input — the plan test can assert its namespace/SA wiring.
resource "aws_eks_pod_identity_association" "prefect_worker" {
  cluster_name    = module.eks.cluster_name
  namespace       = local.prefect_namespace
  service_account = local.worker_service_account
  role_arn        = aws_iam_role.prefect_worker.arn
  tags            = local.tags
}

# The namespace vllm-gpu runs in, created here so the worker's Role/RoleBinding have a
# namespace to land in at cluster-up (they precede `just gpu-deploy`, which is where the
# manifest is otherwise first applied). `just gpu-deploy` re-applies this same namespace
# via `kubectl apply`, which stamps its own labels/annotations
# (last-applied-configuration) on it; ignore_changes there keeps the two owners from
# fighting over the metadata on every plan.
resource "kubernetes_namespace_v1" "slipstream" {
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
  metadata {
    name      = "prefect-worker-vllm-gpu-manage"
    namespace = local.vllm_namespace
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.vllm_gpu_manage.metadata[0].name
  }

  # Cross-namespace subject: the worker's SA lives in prefect, the grant applies in
  # slipstream.
  subject {
    kind      = "ServiceAccount"
    name      = local.worker_service_account
    namespace = local.prefect_namespace
  }
}
