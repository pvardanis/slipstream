# Plan-level tests for the Prefect worker layer on the eks stack (#178): the worker's
# IAM role + least-privilege policy (S3 results bucket read and prefix-scoped writes, SSM
# SendCommand to the bench host), its Pod Identity association, the ServiceAccount RBAC
# scoped to managing deploy/vllm-gpu, and the worker image repository and identity the
# root Application hands Argo CD, which deploys the worker (k8s/argocd/apps). They run
# offline — the aws, helm, and kubernetes providers are mocked, and the bootstrap remote
# state is stubbed with override_data — so the assertions check the install's shape
# without standing a cluster up (ADR-0002 tier-1). A worker actually registering against
# the pool and running a sweep is the cloud tier (#92 / manual).
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
}

mock_provider "aws" {
  alias = "virginia"
}
mock_provider "helm" {}
mock_provider "kubernetes" {}

# Plan against mocked bootstrap remote state instead of reaching for real S3. The
# orchestration ECR repository URL is the worker image's repository.
override_data {
  target = data.terraform_remote_state.bootstrap
  values = {
    outputs = {
      orchestration_image_repo_url    = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
      orchestration_image_repo_arn    = "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream/orchestration"
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

run "worker_layer_shape" {
  command = plan

  # --- IAM trust: Pod Identity, no static keys ---

  # The worker assumes its role through a Pod Identity association; the trust is the
  # pods.eks.amazonaws.com service principal with sts:TagSession alongside AssumeRole
  # (the Pod Identity agent tags the session). No static AWS keys anywhere.
  assert {
    condition     = jsondecode(aws_iam_role.prefect_worker.assume_role_policy).Statement[0].Principal.Service == "pods.eks.amazonaws.com"
    error_message = "The worker role must trust the EKS Pod Identity service principal."
  }
  assert {
    condition     = contains(jsondecode(aws_iam_role.prefect_worker.assume_role_policy).Statement[0].Action, "sts:AssumeRole")
    error_message = "The worker trust policy must allow sts:AssumeRole."
  }
  assert {
    condition     = contains(jsondecode(aws_iam_role.prefect_worker.assume_role_policy).Statement[0].Action, "sts:TagSession")
    error_message = "The worker trust policy must allow sts:TagSession (the Pod Identity agent tags the session)."
  }

  # The association binds the worker's ServiceAccount (in the prefect namespace) to
  # the role. Unlike the EBS CSI association (a module.eks input, unassertable), this
  # is a standalone resource, so the plan can guard its namespace/SA wiring.
  assert {
    condition     = aws_eks_pod_identity_association.prefect_worker.namespace == "prefect"
    error_message = "The Pod Identity association must target the prefect namespace where the worker runs."
  }
  assert {
    condition     = aws_eks_pod_identity_association.prefect_worker.service_account == "prefect-worker"
    error_message = "The Pod Identity association must bind the prefect-worker ServiceAccount."
  }
  # The association's role_arn (= aws_iam_role.prefect_worker.arn) and the policy's role
  # attachment are computed-unknown at plan under the mock provider, so the linkage
  # between association/policy and the worker role is a cloud-tier check (#92), not
  # assertable here — the same limit that defers the trust policy in prefect.tftest.hcl.

  # --- IAM permissions: least-privilege S3 + SSM ---

  # S3: on the bootstrap stack's bench-results bucket, by the exact ARN bootstrap
  # outputs. The host writes the cell result objects the
  # worker reads back; the worker itself writes only its own objects: Prefect's resume
  # state under prefect/, the render tasks' charts under knob-sweep/<run>/charts/ and
  # kv-offload-ab/<run>/charts/, and the kv-ab flow's rung labels, the *.kv_ab.json
  # objects under kv-offload-ab/.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement : s if s.Sid == "ResultsBucketObjects" && contains(s.Action, "s3:GetObject") && s.Resource == "arn:aws:s3:::slipstream-bench-results-abc123/*"]) == 1
    error_message = "The policy must allow s3:GetObject on the bench-results bucket objects, by its exact ARN."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement : s if s.Sid == "ResultsBucketList" && contains(s.Action, "s3:ListBucket") && s.Resource == "arn:aws:s3:::slipstream-bench-results-abc123"]) == 1
    error_message = "The policy must allow s3:ListBucket on the bench-results bucket, by its exact ARN."
  }
  # The worker persists its resume state with persist_result=True: the task result under
  # prefect/results and the cache-key records under prefect/cache-keys. PutObject is scoped
  # to exactly those two prefixes, never the host-owned cell objects under the experiment
  # roots.
  # Resource is normalised to a list so the membership checks never run against a bare
  # string Resource on the other statements (which errors where && does not short-circuit).
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement : s if s.Sid == "PrefectStateObjects" && contains(s.Action, "s3:PutObject") && length(try(tolist(s.Resource), [s.Resource])) == 2 && contains(try(tolist(s.Resource), [s.Resource]), "arn:aws:s3:::slipstream-bench-results-abc123/prefect/results/*") && contains(try(tolist(s.Resource), [s.Resource]), "arn:aws:s3:::slipstream-bench-results-abc123/prefect/cache-keys/*")]) == 1
    error_message = "The policy must allow s3:PutObject scoped to exactly the prefect/results and prefect/cache-keys prefixes."
  }
  # The terminal render tasks upload each plot's PNG under knob-sweep/<run>/charts/ or
  # kv-offload-ab/<run>/charts/ as the run's durable copy (ADR-0018), so the worker needs
  # PutObject on those two charts prefixes. It is a separate statement scoped to exactly
  # them, never the sibling cell-result JSONs the host writes beside them.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement : s if s.Sid == "ChartObjects" && contains(s.Action, "s3:PutObject") && length(try(tolist(s.Resource), [s.Resource])) == 2 && contains(try(tolist(s.Resource), [s.Resource]), "arn:aws:s3:::slipstream-bench-results-abc123/knob-sweep/*/charts/*") && contains(try(tolist(s.Resource), [s.Resource]), "arn:aws:s3:::slipstream-bench-results-abc123/kv-offload-ab/*/charts/*")]) == 1
    error_message = "The policy must allow s3:PutObject on exactly the knob-sweep/<run>/charts/ and kv-offload-ab/<run>/charts/ prefixes for the render tasks' plot uploads."
  }
  # The kv-cache-extension-ab flow writes each rung's label (arm, run, prefix-cache window)
  # as a <cell>.kv_ab.json object beside the host's raw <cell>.json under
  # kv-offload-ab/<run>/<arm>-r<n>/ (#326), so the worker needs PutObject on that suffix: a
  # separate statement allowing that one action on exactly it, never the host's raw results
  # it sits beside.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement : s if s.Sid == "KvAbLabelObjects" && s.Effect == "Allow" && length(s.Action) == 1 && contains(s.Action, "s3:PutObject") && length(try(tolist(s.Resource), [s.Resource])) == 1 && contains(try(tolist(s.Resource), [s.Resource]), "arn:aws:s3:::slipstream-bench-results-abc123/kv-offload-ab/*.kv_ab.json")]) == 1
    error_message = "The policy must allow only s3:PutObject on the kv-offload-ab/ *.kv_ab.json labels for the kv-ab flow's rung labels."
  }
  # Every PutObject resource across all statements is exactly the worker's own objects,
  # so no added statement or resource lets the worker write the host's raw cell results.
  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement : try(tolist(s.Resource), [s.Resource]) if contains(s.Action, "s3:PutObject")])) == toset([
      "arn:aws:s3:::slipstream-bench-results-abc123/prefect/results/*",
      "arn:aws:s3:::slipstream-bench-results-abc123/prefect/cache-keys/*",
      "arn:aws:s3:::slipstream-bench-results-abc123/knob-sweep/*/charts/*",
      "arn:aws:s3:::slipstream-bench-results-abc123/kv-offload-ab/*/charts/*",
      "arn:aws:s3:::slipstream-bench-results-abc123/kv-offload-ab/*.kv_ab.json",
    ])
    error_message = "s3:PutObject must cover exactly the worker's prefect state, charts, and kv-ab label objects."
  }
  # Resource is normalised to a list so a bucket-wide grant is caught whether it is
  # written as a bare string or appended to a statement's resource list.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement : s if contains(s.Action, "s3:PutObject") && length([for r in try(tolist(s.Resource), [s.Resource]) : r if r == "arn:aws:s3:::slipstream-bench-results-abc123/*"]) > 0]) == 0
    error_message = "s3:PutObject must be scoped to the worker's own prefixes, never the bucket-wide objects the host owns."
  }
  # No wildcard or destructive S3 grant slips past the scoped PutObject above (s3:*,
  # s3:DeleteObject, or a bare * would all grant write on the results the host owns).
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement : s if contains(s.Action, "s3:*") || contains(s.Action, "s3:DeleteObject") || contains(s.Action, "*")]) == 0
    error_message = "The policy must not grant wildcard or destructive S3 actions."
  }

  # SSM SendCommand is split in two: the AWS-managed document is authorized
  # unconditionally, and the instance target is narrowed to Project=slipstream tagged
  # instances by condition (the tag on the document is absent, so a single combined
  # statement would deny the send). This scopes the send to the bench host without
  # naming its ephemeral instance id.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement : s if s.Sid == "SsmSendCommandDocument" && contains(s.Action, "ssm:SendCommand") && s.Resource == "arn:aws:ssm:eu-west-1::document/AWS-RunShellScript"]) == 1
    error_message = "The policy must allow ssm:SendCommand on the AWS-RunShellScript document."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement : s if s.Sid == "SsmSendCommandInstances" && contains(s.Action, "ssm:SendCommand") && s.Resource == "arn:aws:ec2:eu-west-1:111122223333:instance/*" && try(s.Condition.StringEquals["ssm:resourceTag/Project"], null) == "slipstream"]) == 1
    error_message = "The SSM instance target must allow ssm:SendCommand on account instances scoped to Project=slipstream."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.prefect_worker.policy).Statement : s if s.Sid == "SsmCommandStatus" && contains(s.Action, "ssm:GetCommandInvocation")]) == 1
    error_message = "The policy must allow ssm:GetCommandInvocation so the worker can poll command completion."
  }

  # --- RBAC: scoped to deploy/vllm-gpu, no cluster-admin ---

  # The Role lives in the slipstream namespace (where vllm-gpu runs), so the grant is
  # namespaced, never cluster-wide.
  assert {
    condition     = kubernetes_role_v1.vllm_gpu_manage.metadata[0].namespace == "slipstream"
    error_message = "The worker Role must live in the slipstream namespace where vllm-gpu runs."
  }

  # get/patch are narrowed to the vllm-gpu Deployment by resourceNames — the worker
  # can read and reconfigure that one Deployment and no other. Both verbs are asserted:
  # get reads the Deployment before patching, patch reconfigures it per sweep point.
  assert {
    condition     = length([for r in kubernetes_role_v1.vllm_gpu_manage.rule : r if contains(r.resources, "deployments") && contains(r.verbs, "get") && contains(r.verbs, "patch") && try(contains(r.resource_names, "vllm-gpu"), false)]) == 1
    error_message = "The Role must scope get/patch on deployments to the vllm-gpu resource name."
  }
  # Reading the rollout needs list/watch on deployments, which resourceNames cannot
  # restrict; the namespace holds only vllm-gpu, so this stays scoped in practice.
  assert {
    condition     = length([for r in kubernetes_role_v1.vllm_gpu_manage.rule : r if contains(r.resources, "deployments") && contains(r.verbs, "watch") && !contains(r.verbs, "patch")]) == 1
    error_message = "The Role must allow list/watch on deployments so the worker can read the rollout."
  }
  # Reading logs (kubectl logs deploy/vllm-gpu) resolves the Deployment's pods, then
  # reads their logs — so it needs pods get/list to find them and pods/log get to read.
  assert {
    condition     = length([for r in kubernetes_role_v1.vllm_gpu_manage.rule : r if contains(r.resources, "pods") && contains(r.verbs, "get") && contains(r.verbs, "list")]) == 1
    error_message = "The Role must allow get/list on pods so the worker can resolve the Deployment's pods for logs."
  }
  assert {
    condition     = length([for r in kubernetes_role_v1.vllm_gpu_manage.rule : r if contains(r.resources, "pods/log") && contains(r.verbs, "get")]) == 1
    error_message = "The Role must allow get on pods/log so the worker can read vllm-gpu logs."
  }
  # No wildcard verbs or cluster-admin.
  assert {
    condition     = length([for r in kubernetes_role_v1.vllm_gpu_manage.rule : r if contains(r.verbs, "*") || contains(r.resources, "*")]) == 0
    error_message = "The worker Role must not use wildcard verbs or resources."
  }

  # The RoleBinding binds that Role to the worker's ServiceAccount, which lives in the
  # prefect namespace (cross-namespace subject).
  assert {
    condition     = kubernetes_role_binding_v1.vllm_gpu_manage.subject[0].name == "prefect-worker"
    error_message = "The RoleBinding must bind the prefect-worker ServiceAccount."
  }
  assert {
    condition     = kubernetes_role_binding_v1.vllm_gpu_manage.subject[0].namespace == "prefect"
    error_message = "The RoleBinding subject must reference the worker SA in the prefect namespace."
  }
  assert {
    condition     = kubernetes_role_binding_v1.vllm_gpu_manage.role_ref[0].name == kubernetes_role_v1.vllm_gpu_manage.metadata[0].name
    error_message = "The RoleBinding must reference the vllm-gpu-manage Role."
  }

  # --- The worker's handoff to Argo CD ---

  # Argo CD deploys the worker (k8s/argocd/apps); Terraform hands it the worker's image
  # repository, from the bootstrap ECR output, through the root Application's values
  # only. Image Updater writes the tag (image_updater.tftest.hcl).
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.orchestrationImageRepository == "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
    error_message = "The worker image repository must come from the bootstrap orchestration_image_repo_url output."
  }
  # The worker's ServiceAccount and namespace Argo CD deploys into are the ones the Pod
  # Identity association binds, so the worker pod gets the role's credentials.
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.serviceAccounts.prefectWorker == aws_eks_pod_identity_association.prefect_worker.service_account
    error_message = "The root Application must hand Argo CD the ServiceAccount the Pod Identity association binds."
  }
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.namespaces.prefect == aws_eks_pod_identity_association.prefect_worker.namespace
    error_message = "The root Application must hand Argo CD the namespace the Pod Identity association binds."
  }
  # The worker reaches the self-hosted server over cluster DNS, no public endpoint.
  assert {
    condition     = yamldecode(helm_release.argocd_apps.values[0]).applications.root.source.helm.valuesObject.prefectApiUrl == "http://prefect-server.prefect.svc.cluster.local:4200/api"
    error_message = "The worker must reach the server over cluster DNS."
  }

  # The slipstream namespace is created here (not just by `just gpu-deploy`) so the
  # Role/RoleBinding have somewhere to land at cluster-up.
  assert {
    condition     = kubernetes_namespace_v1.slipstream.metadata[0].name == "slipstream"
    error_message = "The worker layer must create the slipstream namespace for the vllm-gpu RBAC."
  }
}

# The bootstrap remote state is read from state_bucket, so an empty bucket name (a failed
# bootstrap output lookup) fails the plan at the variable rather than reading state from
# a bucket named "".
run "worker_requires_a_state_bucket" {
  command = plan

  variables {
    state_bucket = ""
  }

  expect_failures = [var.state_bucket]
}

# A bootstrap applied before it created the bench-results bucket has no
# bench_results_bucket_arn output; the worker policy's precondition (and the bench host
# results policy's, which reads the same output) fails with its actionable message
# rather than an unsupported-attribute error.
run "worker_rejects_a_bootstrap_without_the_bench_results_bucket" {
  command = plan

  override_data {
    target = data.terraform_remote_state.bootstrap
    values = {
      outputs = {
        orchestration_image_repo_url    = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
        orchestration_image_repo_arn    = "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream/orchestration"
        bench_image_repo_arn            = "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream-bench"
        sweep_dispatch_role_arn         = "arn:aws:iam::111122223333:role/slipstream-sweep-dispatch"
        grafana_cloud_write_secret_name = "grafana-cloud/write"
        cluster_name                    = "slipstream"
        prefect_namespace               = "prefect"
      }
    }
  }

  expect_failures = [aws_iam_role_policy.prefect_worker, aws_iam_role_policy.bench_host_results]
}
