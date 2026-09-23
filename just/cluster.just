# Cluster and node-pool lifecycle: remote-state bootstrap, EKS create/plan/destroy, and the GPU node pool.

# Connect the bootstrap stack to its remote state (idempotent) so its outputs are
# readable — needed on a fresh checkout, where no local .terraform exists yet.
# Init progress goes to stderr so a recipe that captures a dependent's stdout
# (e.g. `image="$(just _bench-image-ref)"`) gets only the value, not this banner.
_bootstrap-init:
    terraform -chdir={{ bootstrap_dir }} init -input=false >&2

# Create the cluster and point kubectl at it.
cluster-up: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignments so a failed `terraform output` aborts the recipe instead of
    # collapsing to an empty backend/name the next command would use blindly.
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    terraform -chdir={{ eks_dir }} init -input=false -backend-config="bucket=${bucket}"
    terraform -chdir={{ eks_dir }} apply -auto-approve
    name="$(terraform -chdir={{ eks_dir }} output -raw cluster_name)"
    region="$(terraform -chdir={{ eks_dir }} output -raw region)"
    aws eks update-kubeconfig --name "${name}" --region "${region}"
    kubectl get nodes

# Show the cluster changes `just cluster-up` would apply, without provisioning anything.
cluster-plan: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignment so a failed `terraform output` aborts instead of passing an
    # empty bucket into the eks backend (see `cluster-up`).
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    terraform -chdir={{ eks_dir }} init -input=false -backend-config="bucket=${bucket}"
    terraform -chdir={{ eks_dir }} plan

# Apply the GPU node pool (NodePool + EC2NodeClass) and the NVIDIA device plugin.
# Karpenter provisions no node until a pod tolerates the GPU taint and requests
# nvidia.com/gpu — the GPU replica (`just gpu-deploy`, #91) does that.
gpu-pool-up:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v envsubst >/dev/null || { echo "envsubst required (brew install gettext)" >&2; exit 1; }
    # Bare assignments so a failed `terraform output` aborts the recipe (see `cluster-up`);
    # `terraform output -raw` exits 0 with empty stdout when an output resolves to
    # nothing, so the non-empty checks catch that separately — otherwise an empty
    # role and discovery tag would apply an EC2NodeClass that matches no subnet or
    # security group and never provisions. The eks stack is initialised by `just cluster-up`.
    cluster_name="$(terraform -chdir={{ eks_dir }} output -raw cluster_name)"
    [[ -n "${cluster_name}" ]] || { echo "eks output cluster_name is empty" >&2; exit 1; }
    node_role="$(terraform -chdir={{ eks_dir }} output -raw karpenter_node_iam_role_name)"
    [[ -n "${node_role}" ]] || { echo "eks output karpenter_node_iam_role_name is empty" >&2; exit 1; }
    # Substitute only the two known placeholders, so nothing else in the manifest
    # (e.g. a shell-like token in an image tag) is touched.
    CLUSTER_NAME="${cluster_name}" KARPENTER_NODE_ROLE="${node_role}" \
      envsubst '${CLUSTER_NAME} ${KARPENTER_NODE_ROLE}' <{{ gpu_pool_manifests }} \
      | kubectl apply -f -
    kubectl -n kube-system rollout status ds/nvidia-device-plugin-daemonset --timeout=120s

# Remove the GPU node pool and device plugin. Deleting the NodePool makes Karpenter
# drain and terminate any GPU instance it launched — instances that live outside
# Terraform state, so reaping them here keeps `just cluster-down` from racing the controller
# and orphaning a billing g5. Objects delete by name, so the manifest's unsubstituted
# placeholders are irrelevant and no terraform lookup is needed — teardown never
# blocks on eks state being reachable.
gpu-pool-down:
    kubectl delete -f {{ gpu_pool_manifests }} --ignore-not-found

# Destroy the cluster (the bootstrap state bucket is left intact).
cluster-down:
    terraform -chdir={{ eks_dir }} destroy -auto-approve

# Apply the state-bootstrap stack once, before the first `just cluster-up`. Assumes the
# state bucket already exists (state lives in it, see backend.tf). A brand-new
# environment bootstraps the bucket first with the two-step in ADR-0005.
bootstrap:
    terraform -chdir={{ bootstrap_dir }} init -input=false
    terraform -chdir={{ bootstrap_dir }} apply -auto-approve
