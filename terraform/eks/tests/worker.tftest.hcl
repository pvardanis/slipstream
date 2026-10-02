# Plan-level tests for the Prefect worker layer on the eks stack (#178): the worker's
# IAM role + least-privilege policy (S3 results bucket read, SSM SendCommand to the
# bench host), its Pod Identity association, the ServiceAccount RBAC scoped to
# managing deploy/vllm-gpu, and the prefect-worker Helm release. They run offline —
# the aws, helm, and kubernetes providers are mocked, and the bootstrap remote state
# and the orchestration ECR image are stubbed with override_data — so the assertions
# check the install's shape without standing a cluster up (ADR-0002 tier-1). A worker
# actually registering against the pool and running a sweep is the cloud tier (#92 /
# manual).
#
# The permission policy and trust policy are jsonencode()'d inline (not
# aws_iam_policy_document data sources), so their statements are known at plan and
# assertable here — the mock provider blanks aws_iam_policy_document to empty JSON
# (see the note in prefect.tftest.hcl), which would hide their contents.

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
  # The orchestration image the worker runs: the :main pointer resolves to one manifest
  # carrying exactly its own content-sha tag, so image_tags is [main, <sha>]. The stack
  # picks the non-main tag to pin the Deployment to an immutable ref.
  mock_data "aws_ecr_image" {
    defaults = { image_tags = ["main", "orchestrator-abc123"] }
  }
}

mock_provider "aws" {
  alias = "virginia"
}
mock_provider "helm" {}
mock_provider "kubernetes" {}

run "worker_layer_shape" {
  command = plan

  variables {
    enable_prefect_worker = true
    state_bucket          = "slipstream-tfstate-abc123"
  }

  # Plan against mocked bootstrap remote state instead of reaching for real S3. The
  # orchestration ECR repository URL is the worker image's repository; the content-sha
  # tag is resolved from ECR (mock_data aws_ecr_image above).
  override_data {
    target = data.terraform_remote_state.bootstrap[0]
    values = {
      outputs = {
        orchestration_image_repo_url = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
      }
    }
  }

  # --- IAM trust: Pod Identity, no static keys ---

  # The worker assumes its role through a Pod Identity association; the trust is the
  # pods.eks.amazonaws.com service principal with sts:TagSession alongside AssumeRole
  # (the Pod Identity agent tags the session). No static AWS keys anywhere.
  assert {
    condition     = jsondecode(aws_iam_role.prefect_worker[0].assume_role_policy).Statement[0].Principal.Service == "pods.eks.amazonaws.com"
    error_message = "The worker role must trust the EKS Pod Identity service principal."
  }
  assert {
    condition     = contains(jsondecode(aws_iam_role.prefect_worker[0].assume_role_policy).Statement[0].Action, "sts:AssumeRole")
    error_message = "The worker trust policy must allow sts:AssumeRole."
  }
  assert {
    condition     = contains(jsondecode(aws_iam_role.prefect_worker[0].assume_role_policy).Statement[0].Action, "sts:TagSession")
    error_message = "The worker trust policy must allow sts:TagSession (the Pod Identity agent tags the session)."
  }

  # The association binds the worker's ServiceAccount (in the prefect namespace) to
  # the role. Unlike the EBS CSI association (a module.eks input, unassertable), this
  # is a standalone resource, so the plan can guard its namespace/SA wiring.
  assert {
    condition     = aws_eks_pod_identity_association.prefect_worker[0].namespace == "prefect"
    error_message = "The Pod Identity association must target the prefect namespace where the worker runs."
  }
  assert {
    condition     = aws_eks_pod_identity_association.prefect_worker[0].service_account == "prefect-worker"
    error_message = "The Pod Identity association must bind the prefect-worker ServiceAccount."
  }
  # The association's role_arn (= aws_iam_role.prefect_worker.arn) and the policy's role
  # attachment are computed-unknown at plan under the mock provider, so the linkage
  # between association/policy and the worker role is a cloud-tier check (#92), not
  # assertable here — the same limit that defers the trust policy in prefect.tftest.hcl.

  # --- IAM permissions: least-privilege S3 + SSM ---

  # S3: on the bench-endpoint results bucket. The bucket's name carries a random suffix
  # and lives in a stack this one cannot reference without a dependency cycle, so it is
  # scoped by ARN prefix, not an exact ARN. The host writes the sweep result objects the
  # worker reads back; the worker itself writes only Prefect's own resume state (the
  # persisted task result and cache-key records) under the prefect/ prefixes.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker[0].policy).Statement : s if s.Sid == "ResultsBucketObjects" && contains(s.Action, "s3:GetObject") && s.Resource == "arn:aws:s3:::slipstream-bench-endpoint-results-*/*"]) == 1
    error_message = "The policy must allow s3:GetObject on the results bucket objects, scoped by ARN prefix."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker[0].policy).Statement : s if s.Sid == "ResultsBucketList" && contains(s.Action, "s3:ListBucket") && s.Resource == "arn:aws:s3:::slipstream-bench-endpoint-results-*"]) == 1
    error_message = "The policy must allow s3:ListBucket on the results bucket, scoped by ARN prefix."
  }
  # The worker persists its resume state with persist_result=True: the task result under
  # prefect/results and the cache-key records under prefect/cache-keys. PutObject is scoped
  # to exactly those two prefixes, never the host-owned sweep objects at the bucket root.
  # Resource is normalised to a list so the membership checks never run against a bare
  # string Resource on the other statements (which errors where && does not short-circuit).
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker[0].policy).Statement : s if s.Sid == "PrefectStateObjects" && contains(s.Action, "s3:PutObject") && length(try(tolist(s.Resource), [s.Resource])) == 2 && contains(try(tolist(s.Resource), [s.Resource]), "arn:aws:s3:::slipstream-bench-endpoint-results-*/prefect/results/*") && contains(try(tolist(s.Resource), [s.Resource]), "arn:aws:s3:::slipstream-bench-endpoint-results-*/prefect/cache-keys/*")]) == 1
    error_message = "The policy must allow s3:PutObject scoped to exactly the prefect/results and prefect/cache-keys prefixes."
  }
  # Resource is normalised to a list so a bucket-wide grant is caught whether it is
  # written as a bare string or appended to a statement's resource list.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker[0].policy).Statement : s if contains(s.Action, "s3:PutObject") && length([for r in try(tolist(s.Resource), [s.Resource]) : r if r == "arn:aws:s3:::slipstream-bench-endpoint-results-*/*"]) > 0]) == 0
    error_message = "s3:PutObject must be scoped to the Prefect prefixes, never the bucket-wide objects the host owns."
  }
  # No wildcard or destructive S3 grant slips past the scoped PutObject above (s3:*,
  # s3:DeleteObject, or a bare * would all grant write on the results the host owns).
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker[0].policy).Statement : s if contains(s.Action, "s3:*") || contains(s.Action, "s3:DeleteObject") || contains(s.Action, "*")]) == 0
    error_message = "The policy must not grant wildcard or destructive S3 actions."
  }

  # SSM SendCommand is split in two: the AWS-managed document is authorized
  # unconditionally, and the instance target is narrowed to Project=slipstream tagged
  # instances by condition (the tag on the document is absent, so a single combined
  # statement would deny the send). This scopes the send to the bench host without
  # naming its ephemeral instance id.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker[0].policy).Statement : s if s.Sid == "SsmSendCommandDocument" && contains(s.Action, "ssm:SendCommand") && s.Resource == "arn:aws:ssm:eu-west-1::document/AWS-RunShellScript"]) == 1
    error_message = "The policy must allow ssm:SendCommand on the AWS-RunShellScript document."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker[0].policy).Statement : s if s.Sid == "SsmSendCommandInstances" && contains(s.Action, "ssm:SendCommand") && s.Resource == "arn:aws:ec2:eu-west-1:111122223333:instance/*" && try(s.Condition.StringEquals["ssm:resourceTag/Project"], null) == "slipstream"]) == 1
    error_message = "The SSM instance target must allow ssm:SendCommand on account instances scoped to Project=slipstream."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker[0].policy).Statement : s if s.Sid == "SsmCommandStatus" && contains(s.Action, "ssm:GetCommandInvocation")]) == 1
    error_message = "The policy must allow ssm:GetCommandInvocation so the worker can poll command completion."
  }

  # --- RBAC: scoped to deploy/vllm-gpu, no cluster-admin ---

  # The Role lives in the slipstream namespace (where vllm-gpu runs), so the grant is
  # namespaced, never cluster-wide.
  assert {
    condition     = kubernetes_role_v1.vllm_gpu_manage[0].metadata[0].namespace == "slipstream"
    error_message = "The worker Role must live in the slipstream namespace where vllm-gpu runs."
  }

  # get/patch are narrowed to the vllm-gpu Deployment by resourceNames — the worker
  # can read and reconfigure that one Deployment and no other. Both verbs are asserted:
  # get reads the Deployment before patching, patch reconfigures it per sweep point.
  assert {
    condition     = length([for r in kubernetes_role_v1.vllm_gpu_manage[0].rule : r if contains(r.resources, "deployments") && contains(r.verbs, "get") && contains(r.verbs, "patch") && try(contains(r.resource_names, "vllm-gpu"), false)]) == 1
    error_message = "The Role must scope get/patch on deployments to the vllm-gpu resource name."
  }
  # Reading the rollout needs list/watch on deployments, which resourceNames cannot
  # restrict; the namespace holds only vllm-gpu, so this stays scoped in practice.
  assert {
    condition     = length([for r in kubernetes_role_v1.vllm_gpu_manage[0].rule : r if contains(r.resources, "deployments") && contains(r.verbs, "watch") && !contains(r.verbs, "patch")]) == 1
    error_message = "The Role must allow list/watch on deployments so the worker can read the rollout."
  }
  # Reading logs (kubectl logs deploy/vllm-gpu) resolves the Deployment's pods, then
  # reads their logs — so it needs pods get/list to find them and pods/log get to read.
  assert {
    condition     = length([for r in kubernetes_role_v1.vllm_gpu_manage[0].rule : r if contains(r.resources, "pods") && contains(r.verbs, "get") && contains(r.verbs, "list")]) == 1
    error_message = "The Role must allow get/list on pods so the worker can resolve the Deployment's pods for logs."
  }
  assert {
    condition     = length([for r in kubernetes_role_v1.vllm_gpu_manage[0].rule : r if contains(r.resources, "pods/log") && contains(r.verbs, "get")]) == 1
    error_message = "The Role must allow get on pods/log so the worker can read vllm-gpu logs."
  }
  # No wildcard verbs or cluster-admin.
  assert {
    condition     = length([for r in kubernetes_role_v1.vllm_gpu_manage[0].rule : r if contains(r.verbs, "*") || contains(r.resources, "*")]) == 0
    error_message = "The worker Role must not use wildcard verbs or resources."
  }

  # The RoleBinding binds that Role to the worker's ServiceAccount, which lives in the
  # prefect namespace (cross-namespace subject).
  assert {
    condition     = kubernetes_role_binding_v1.vllm_gpu_manage[0].subject[0].name == "prefect-worker"
    error_message = "The RoleBinding must bind the prefect-worker ServiceAccount."
  }
  assert {
    condition     = kubernetes_role_binding_v1.vllm_gpu_manage[0].subject[0].namespace == "prefect"
    error_message = "The RoleBinding subject must reference the worker SA in the prefect namespace."
  }
  assert {
    condition     = kubernetes_role_binding_v1.vllm_gpu_manage[0].role_ref[0].name == kubernetes_role_v1.vllm_gpu_manage[0].metadata[0].name
    error_message = "The RoleBinding must reference the vllm-gpu-manage Role."
  }

  # --- Helm release: the worker itself ---

  # The worker deploys into the prefect namespace — alongside the server, where its SA
  # and Pod Identity association live.
  assert {
    condition     = helm_release.prefect_worker[0].namespace == "prefect"
    error_message = "The worker must deploy into the prefect namespace."
  }
  assert {
    condition     = helm_release.prefect_worker[0].repository == "https://prefecthq.github.io/prefect-helm"
    error_message = "The worker must install from the upstream prefect-helm repository."
  }
  assert {
    condition     = helm_release.prefect_worker[0].chart == "prefect-worker"
    error_message = "Helm release must deploy the prefect-worker chart."
  }
  assert {
    condition     = helm_release.prefect_worker[0].version == var.prefect_worker_chart_version
    error_message = "Chart version must be pinned to var.prefect_worker_chart_version."
  }
  assert {
    condition     = helm_release.prefect_worker[0].wait == true
    error_message = "The worker release must wait for the Deployment so a broken install fails the apply."
  }
  assert {
    condition     = helm_release.prefect_worker[0].atomic == true
    error_message = "The worker release must be atomic so a failed install rolls back instead of leaving a half-up worker."
  }

  # A process worker (not kubernetes): the sweep runs as a subprocess in the worker
  # pod, matching the serial single-GPU sweep (ADR-0015 amendment).
  assert {
    condition     = yamldecode(helm_release.prefect_worker[0].values[0]).worker.config.type == "process"
    error_message = "The worker must be a process worker, not kubernetes."
  }
  assert {
    condition     = yamldecode(helm_release.prefect_worker[0].values[0]).worker.config.workPool == var.prefect_work_pool
    error_message = "The worker must poll the sweep work pool (var.prefect_work_pool)."
  }
  # A single replica: one worker draining a concurrency-1 pool, so the single-GPU sweep
  # never runs two points at once.
  assert {
    condition     = yamldecode(helm_release.prefect_worker[0].values[0]).worker.replicaCount == 1
    error_message = "The worker must run a single replica for the serial single-GPU sweep."
  }
  # Connects to the in-cluster self-hosted server over cluster DNS, no public endpoint.
  assert {
    condition     = yamldecode(helm_release.prefect_worker[0].values[0]).worker.apiConfig == "selfHostedServer"
    error_message = "The worker must target the self-hosted in-cluster server."
  }
  assert {
    condition     = yamldecode(helm_release.prefect_worker[0].values[0]).worker.selfHostedServerApiConfig.apiUrl == "http://prefect-server.prefect.svc.cluster.local:4200/api"
    error_message = "The worker must reach the server over cluster DNS."
  }
  # Runs the orchestration image: repository from the bootstrap ECR output, tag the
  # content-sha resolved from the :main image (the non-main tag on that manifest).
  assert {
    condition     = yamldecode(helm_release.prefect_worker[0].values[0]).worker.image.repository == "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
    error_message = "The worker image repository must come from the bootstrap orchestration_image_repo_url output."
  }
  # The ECR lookup keys off the repository path, not the full registry URL — trimprefix
  # must strip the host. A bug there (wrong split, leftover slash) would only surface as a
  # real-ECR lookup miss, so pin the transform here where the input is known at plan.
  assert {
    condition     = data.aws_ecr_image.orchestration[0].repository_name == "slipstream/orchestration"
    error_message = "The ECR lookup must strip the registry host to the repository path (slipstream/orchestration)."
  }
  assert {
    condition     = yamldecode(helm_release.prefect_worker[0].values[0]).worker.image.prefectTag == "orchestrator-abc123"
    error_message = "The worker image tag must be the content-sha tag resolved from the orchestration :main image."
  }
  # The ref is an immutable content-sha tag, so IfNotPresent is safe and correct: the
  # tag uniquely identifies content, so a cached image on a node is always the right one
  # (a floating :main + IfNotPresent would silently run a stale cached image).
  assert {
    condition     = yamldecode(helm_release.prefect_worker[0].values[0]).worker.image.pullPolicy == "IfNotPresent"
    error_message = "The worker image pullPolicy must be IfNotPresent — the content-sha tag is immutable."
  }
  # The chart creates the SA (which Pod Identity binds); RBAC is owned by this stack,
  # so the chart's own Role/RoleBinding are off — the chart's default role grants
  # kubernetes-worker permissions a process worker does not need.
  assert {
    condition     = yamldecode(helm_release.prefect_worker[0].values[0]).serviceAccount.create == true
    error_message = "The chart must create the worker ServiceAccount (which Pod Identity binds)."
  }
  assert {
    condition     = yamldecode(helm_release.prefect_worker[0].values[0]).serviceAccount.name == "prefect-worker"
    error_message = "The chart must name the ServiceAccount prefect-worker to match the Pod Identity association."
  }
  assert {
    condition     = yamldecode(helm_release.prefect_worker[0].values[0]).role.create == false
    error_message = "The chart's own Role must be off — this stack owns the scoped RBAC."
  }
  assert {
    condition     = yamldecode(helm_release.prefect_worker[0].values[0]).rolebinding.create == false
    error_message = "The chart's own RoleBinding must be off — this stack owns the scoped RBAC."
  }

  # The slipstream namespace is created here (not just by `just gpu-deploy`) so the
  # Role/RoleBinding have somewhere to land at cluster-up.
  assert {
    condition     = kubernetes_namespace_v1.slipstream[0].metadata[0].name == "slipstream"
    error_message = "The worker layer must create the slipstream namespace for the vllm-gpu RBAC."
  }
}

# With the worker disabled the layer is absent entirely, so `just cluster-up` stands the
# cluster up before the orchestration image exists (the image ships in its own build
# stack). Enabling the worker is what brings it up.
run "worker_absent_when_disabled" {
  command = plan

  variables {
    enable_prefect_worker = false
    state_bucket          = "slipstream-tfstate-abc123"
  }

  assert {
    condition     = length(helm_release.prefect_worker) == 0
    error_message = "A disabled worker must leave the worker release uncreated."
  }
  assert {
    condition     = length(aws_iam_role.prefect_worker) == 0
    error_message = "A disabled worker must leave the worker IAM role uncreated."
  }
  assert {
    condition     = length(aws_iam_role_policy.prefect_worker) == 0
    error_message = "A disabled worker must leave the worker IAM policy uncreated."
  }
  assert {
    condition     = length(aws_eks_pod_identity_association.prefect_worker) == 0
    error_message = "A disabled worker must leave the Pod Identity association uncreated."
  }
  assert {
    condition     = length(kubernetes_role_v1.vllm_gpu_manage) == 0
    error_message = "A disabled worker must leave the worker RBAC uncreated."
  }
  assert {
    condition     = length(kubernetes_role_binding_v1.vllm_gpu_manage) == 0
    error_message = "A disabled worker must leave the worker RoleBinding uncreated."
  }
  assert {
    condition     = length(kubernetes_namespace_v1.slipstream) == 0
    error_message = "A disabled worker must leave the slipstream namespace uncreated."
  }
  # A disabled worker reads neither the bootstrap remote state nor the ECR image.
  assert {
    condition     = length(data.terraform_remote_state.bootstrap) == 0
    error_message = "A disabled worker must not read the bootstrap remote state."
  }
  assert {
    condition     = length(data.aws_ecr_image.orchestration) == 0
    error_message = "A disabled worker must not resolve the orchestration image from ECR."
  }
}

# The :main image must carry exactly one content-sha tag for the pin to be unambiguous.
# If the resolve finds none (only :main present), the enabled worker fails the plan loudly
# rather than deploying with a blank tag Helm would resolve to something unintended.
run "worker_requires_a_content_tag" {
  command = plan

  variables {
    enable_prefect_worker = true
    state_bucket          = "slipstream-tfstate-abc123"
  }

  override_data {
    target = data.terraform_remote_state.bootstrap[0]
    values = {
      outputs = {
        orchestration_image_repo_url = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
      }
    }
  }

  override_data {
    target = data.aws_ecr_image.orchestration[0]
    values = {
      image_tags = ["main"]
    }
  }

  expect_failures = [data.aws_ecr_image.orchestration]
}

# The other side of the ==1 postcondition: a :main manifest carrying two content-sha tags
# is as ambiguous as none — [0] would pin an arbitrary one — so the enabled worker fails
# the plan loudly rather than guessing which tag to deploy.
run "worker_rejects_multiple_content_tags" {
  command = plan

  variables {
    enable_prefect_worker = true
    state_bucket          = "slipstream-tfstate-abc123"
  }

  override_data {
    target = data.terraform_remote_state.bootstrap[0]
    values = {
      outputs = {
        orchestration_image_repo_url = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
      }
    }
  }

  override_data {
    target = data.aws_ecr_image.orchestration[0]
    values = {
      image_tags = ["main", "orchestrator-abc123", "orchestrator-def456"]
    }
  }

  expect_failures = [data.aws_ecr_image.orchestration]
}
