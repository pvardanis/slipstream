# Plan-level tests for the sweep-dispatch access on the eks stack (#180): the EKS access
# entry that maps the bootstrap sweep-dispatch role to a Kubernetes group, and the
# namespaced Role/RoleBinding that grant that group only what `kubectl port-forward
# svc/prefect-server` needs in the prefect namespace. They run offline — the aws, helm,
# and kubernetes providers are mocked and the bootstrap remote state is stubbed with
# override_data — so the assertions check the grant's shape without a cluster. A real
# dispatch assuming the role and port-forwarding is the cloud tier (manual, post-merge).

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
  mock_data "aws_ecr_image" {
    defaults = { image_tags = ["main", "orchestrator-abc123"] }
  }
}

mock_provider "aws" {
  alias = "virginia"
}
mock_provider "helm" {}
mock_provider "kubernetes" {}

# Plan against mocked bootstrap remote state instead of reaching for real S3. The
# orchestration ECR repository URL is the worker image's repository; the content-sha
# tag is resolved from ECR (mock_data aws_ecr_image above).
override_data {
  target = data.terraform_remote_state.bootstrap
  values = {
    outputs = {
      orchestration_image_repo_url = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
      sweep_dispatch_role_arn      = "arn:aws:iam::111122223333:role/slipstream-sweep-dispatch"
      cluster_name                 = "slipstream"
      prefect_namespace            = "prefect"
    }
  }
}

variables {
  state_bucket = "slipstream-tfstate-abc123"
}

run "sweep_dispatch_access_shape" {
  command = plan

  # --- Access entry: the bootstrap role, mapped to a group, no AWS-managed policy ---

  # The principal is the role the sweep-dispatch workflow assumes, read from the bootstrap
  # output rather than restated here.
  assert {
    condition     = aws_eks_access_entry.sweep_dispatch.principal_arn == "arn:aws:iam::111122223333:role/slipstream-sweep-dispatch"
    error_message = "The access entry must map the bootstrap sweep_dispatch_role_arn output."
  }
  assert {
    condition     = aws_eks_access_entry.sweep_dispatch.type == "STANDARD"
    error_message = "The access entry must be a STANDARD entry."
  }
  # The entry's only Kubernetes identity is the one group the RoleBinding below grants.
  assert {
    condition     = length(aws_eks_access_entry.sweep_dispatch.kubernetes_groups) == 1 && contains(aws_eks_access_entry.sweep_dispatch.kubernetes_groups, "sweep-dispatch")
    error_message = "The access entry must map the role to the sweep-dispatch group and no other."
  }

  # --- RBAC: port-forward to the Prefect server, nothing wider ---

  assert {
    condition     = kubernetes_role_v1.sweep_dispatch.metadata[0].namespace == "prefect"
    error_message = "The sweep-dispatch Role must live in the prefect namespace, where the server runs."
  }
  # `kubectl port-forward svc/prefect-server` reads the Service to resolve its backing pod,
  # reads that pod, then opens the forward on the pods/portforward subresource.
  assert {
    condition     = length([for r in kubernetes_role_v1.sweep_dispatch.rule : r if contains(r.resources, "services") && contains(r.resources, "pods") && contains(r.verbs, "get") && contains(r.verbs, "list")]) == 1
    error_message = "The Role must allow get/list on services and pods so port-forward can resolve the server pod."
  }
  # The forward is authorized as `get` when kubectl opens it over WebSockets and as
  # `create` over SPDY (and on the WebSocket upgrade where the cluster also checks
  # create), so both verbs are needed and no others.
  assert {
    condition     = length([for r in kubernetes_role_v1.sweep_dispatch.rule : r if length(r.resources) == 1 && contains(r.resources, "pods/portforward") && length(r.verbs) == 2 && contains(r.verbs, "get") && contains(r.verbs, "create")]) == 1
    error_message = "The Role must allow get and create on pods/portforward, and nothing else on it."
  }
  # Every verb outside the portforward rule is a read, so the group can open the forward
  # and nothing else: no writes, deletes, exec, or wildcards.
  assert {
    condition     = length([for r in kubernetes_role_v1.sweep_dispatch.rule : r if !(length(r.resources) == 1 && contains(r.resources, "pods/portforward")) && length(setsubtract(r.verbs, ["get", "list"])) > 0]) == 0
    error_message = "Apart from pods/portforward, the Role must grant only get/list."
  }
  assert {
    condition     = length([for r in kubernetes_role_v1.sweep_dispatch.rule : r if contains(r.verbs, "*") || contains(r.resources, "*")]) == 0
    error_message = "The sweep-dispatch Role must not use wildcard verbs or resources."
  }
  # Every rule is on the core API group, so "services" and "pods" mean the core kinds and
  # no wildcard group widens them.
  assert {
    condition     = alltrue([for r in kubernetes_role_v1.sweep_dispatch.rule : length(r.api_groups) == 1 && contains(r.api_groups, "")])
    error_message = "Every sweep-dispatch Role rule must be on the core API group only."
  }

  # The RoleBinding grants that Role to the access entry's group, in the same namespace.
  assert {
    condition     = kubernetes_role_binding_v1.sweep_dispatch.metadata[0].namespace == "prefect"
    error_message = "The sweep-dispatch RoleBinding must live in the prefect namespace."
  }
  assert {
    condition     = length(kubernetes_role_binding_v1.sweep_dispatch.subject) == 1 && kubernetes_role_binding_v1.sweep_dispatch.subject[0].kind == "Group" && kubernetes_role_binding_v1.sweep_dispatch.subject[0].name == "sweep-dispatch"
    error_message = "The RoleBinding must bind only the sweep-dispatch group the access entry maps."
  }
  # A namespaced Role, never a ClusterRole, so the binding cannot reach past the
  # prefect namespace's grant.
  assert {
    condition     = kubernetes_role_binding_v1.sweep_dispatch.role_ref[0].kind == "Role" && kubernetes_role_binding_v1.sweep_dispatch.role_ref[0].name == kubernetes_role_v1.sweep_dispatch.metadata[0].name
    error_message = "The RoleBinding must reference the namespaced sweep-dispatch Role."
  }
}

# gh-vars sets EKS_CLUSTER_NAME and PREFECT_NAMESPACE from the bootstrap outputs, so a
# bootstrap that names a different cluster or namespace than this stack would point the
# sweep-dispatch workflow at nothing. The plan fails instead of deploying that mismatch.
run "sweep_dispatch_rejects_a_different_bootstrap_cluster" {
  command = plan

  override_data {
    target = data.terraform_remote_state.bootstrap
    values = {
      outputs = {
        orchestration_image_repo_url = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
        sweep_dispatch_role_arn      = "arn:aws:iam::111122223333:role/slipstream-sweep-dispatch"
        cluster_name                 = "other-cluster"
        prefect_namespace            = "prefect"
      }
    }
  }

  expect_failures = [aws_eks_access_entry.sweep_dispatch]
}

run "sweep_dispatch_rejects_a_different_bootstrap_namespace" {
  command = plan

  override_data {
    target = data.terraform_remote_state.bootstrap
    values = {
      outputs = {
        orchestration_image_repo_url = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
        sweep_dispatch_role_arn      = "arn:aws:iam::111122223333:role/slipstream-sweep-dispatch"
        cluster_name                 = "slipstream"
        prefect_namespace            = "other-namespace"
      }
    }
  }

  expect_failures = [kubernetes_role_v1.sweep_dispatch]
}
