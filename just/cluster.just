# Cluster and node-pool lifecycle: remote-state bootstrap, EKS create/plan/destroy, and the GPU node pool.

# Connect the bootstrap stack to its remote state (idempotent) so its outputs are
# readable — needed on a fresh checkout, where no local .terraform exists yet.
# Init progress goes to stderr so a recipe that captures a dependent's stdout
# (e.g. `image="$(just _bench-image-ref)"`) gets only the value, not this banner.
_bootstrap-init:
    terraform -chdir={{ bootstrap_dir }} init -input=false >&2

# Create the cluster and point kubectl at it.
[group('cluster')]
cluster-up: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignment so a failed `terraform output` aborts the recipe; the non-empty check
    # then catches the exit-0-empty-stdout case (see gpu-pool-up) an empty backend/name the
    # next command would otherwise use blindly.
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    [[ -n "${bucket}" ]] || { echo "bootstrap output state_bucket_name is empty" >&2; exit 1; }
    terraform -chdir={{ eks_dir }} init -input=false -backend-config="bucket=${bucket}"
    # The worker requires the orchestration image published: Image Updater rolls it to
    # the newest content-sha tag in the bootstrap orchestration ECR repo, so with none
    # there the worker never comes up. The check fails a missing :main with the publish
    # step.
    {{ justfile_directory() }}/orchestration/orchestration-image-check.sh {{ bootstrap_dir }}
    terraform -chdir={{ eks_dir }} apply -auto-approve -var="state_bucket=${bucket}"
    name="$(terraform -chdir={{ eks_dir }} output -raw cluster_name)"
    region="$(terraform -chdir={{ eks_dir }} output -raw region)"
    aws eks update-kubeconfig --name "${name}" --region "${region}"
    kubectl get nodes
    # The eks stack hands Argo CD the root Application, but Helm's wait does not cover
    # custom resources, so the apply succeeds before Argo CD has fetched or synced
    # anything. Wait for the root to report Synced and Healthy, so a missing revision,
    # a chart that fails to render or a project denial fails cluster-up here. A Synced
    # root has written every child Application, and the root's health does not follow
    # theirs, so then wait for every Application: a worker that cannot pull its image or
    # crashloops fails cluster-up rather than the first dispatch.
    wait_synced_and_healthy() {
      local check field want
      for check in sync=Synced health=Healthy; do
        field="${check%%=*}"
        want="${check#*=}"
        kubectl -n argocd wait "$@" --for=jsonpath="{.status.${field}.status}=${want}" --timeout=300s || {
          echo "Argo CD Application(s) not ${want} after 300s (kubectl -n argocd wait $*); see the status.conditions: kubectl -n argocd get applications -o yaml" >&2
          exit 1
        }
      done
    }
    # The worker's image tag has one writer, Image Updater, which writes it onto the
    # worker's Application as Helm parameters on its first cycle after the worker app
    # appears; until then the worker runs the chart's default tag and cannot pull it. So
    # once the root syncs, wait for those parameters, then for the worker's Application to
    # report the image they name before waiting on every Application, which would
    # otherwise still report a previous sync's Synced and Healthy. The worker's container
    # is the app's only image, so it is images[0] once any older pod is gone.
    wait_synced_and_healthy application/root
    tag_parameter='{.spec.source.helm.parameters[?(@.name=="worker.image.prefectTag")].value}'
    kubectl -n argocd wait application/prefect-worker --for=jsonpath="${tag_parameter}" --timeout=300s || {
      echo "Image Updater wrote no image tag onto Argo CD's prefect-worker Application after 300s; see its logs: kubectl -n argocd logs deploy/argocd-image-updater-controller" >&2
      exit 1
    }
    worker_image="$(kubectl -n argocd get application/prefect-worker -o jsonpath='{.spec.source.helm.parameters[?(@.name=="worker.image.repository")].value}:'"${tag_parameter}")"
    [[ "${worker_image}" != :* && "${worker_image}" != *: ]] || {
      echo "Argo CD's prefect-worker Application carries no worker.image.repository or worker.image.prefectTag Helm parameter (got '${worker_image}'); see the Image Updater annotations in k8s/argocd/apps/templates/prefect-worker.yaml" >&2
      exit 1
    }
    kubectl -n argocd wait application/prefect-worker --for=jsonpath="{.status.summary.images[0]}=${worker_image}" --timeout=300s || {
      echo "Argo CD's prefect-worker Application did not report image ${worker_image} after 300s; see its status: kubectl -n argocd get application prefect-worker -o yaml" >&2
      exit 1
    }
    wait_synced_and_healthy applications --all

# Show the cluster changes `just cluster-up` would apply, without provisioning anything.
[group('cluster')]
cluster-plan: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignment + non-empty check so a failed or empty `terraform output` aborts
    # instead of passing an empty bucket into the eks backend (see `cluster-up`).
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    [[ -n "${bucket}" ]] || { echo "bootstrap output state_bucket_name is empty" >&2; exit 1; }
    terraform -chdir={{ eks_dir }} init -input=false -backend-config="bucket=${bucket}"
    {{ justfile_directory() }}/orchestration/orchestration-image-check.sh {{ bootstrap_dir }}
    terraform -chdir={{ eks_dir }} plan -var="state_bucket=${bucket}"

# Needs `gh` authenticated and the bootstrap stack applied (`just bootstrap`).
# Set the GitHub repo variables the CI workflows read from the bootstrap outputs.
[group('cluster')]
gh-vars: _bootstrap-init
    {{ justfile_directory() }}/scripts/gh-vars.sh {{ bootstrap_dir }}

# Apply the GPU node pool (NodePool + EC2NodeClass) and the NVIDIA device plugin.
# Karpenter provisions no node until a pod tolerates the GPU taint and requests
# nvidia.com/gpu — the GPU replica (`just gpu-deploy`, #91) does that.
[group('cluster')]
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
# blocks on eks state being reachable. A re-run after cluster-down leaves the API
# endpoint no longer resolving; kubectl then dies on a dial error rather than the no-op
# --ignore-not-found gives for a missing object. A gone cluster has no NodePool to
# delete and no g5 for Karpenter to reap, so probe reachability and skip when it is gone.
[group('cluster')]
gpu-pool-down:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! kubectl cluster-info --request-timeout=5s >/dev/null 2>&1; then
      echo "gpu-pool-down: cluster API unreachable; nothing to reap (already torn down)" >&2
      exit 0
    fi
    kubectl delete -f {{ gpu_pool_manifests }} --ignore-not-found

# Destroy the cluster (the bootstrap state bucket is left intact). Initialises the eks
# backend first, exactly as `just cluster-up` does: destroy needs the s3 backend
# configured, and a fresh checkout (or a reconfigured backend) has no `.terraform/`, so
# a bare destroy aborts with "Backend initialization required" and leaves the cluster
# billing.
[group('cluster')]
cluster-down: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignment + non-empty check so a failed or empty `terraform output` aborts
    # instead of collapsing to an empty backend the destroy would then reject.
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    [[ -n "${bucket}" ]] || { echo "bootstrap output state_bucket_name is empty" >&2; exit 1; }
    terraform -chdir={{ eks_dir }} init -input=false -backend-config="bucket=${bucket}"
    # -refresh=false destroys from the recorded state without reading the bootstrap remote
    # state or the orchestration image, so a missing image or unreadable bootstrap state
    # cannot leave the cluster up and billing.
    terraform -chdir={{ eks_dir }} destroy -auto-approve -refresh=false -var="state_bucket=${bucket}"

# Needs `aws` authenticated to the bootstrap account. Covers the free stack and its URL for
# the grafana stack, its Assume Role external ID, a bootstrap apply, and the four tokens
# written to Secrets Manager.
# Walk through Grafana Cloud's one-time setup (ADR-0026).
[group('cluster')]
grafana-cloud-setup:
    {{ justfile_directory() }}/scripts/grafana-cloud-setup.sh {{ bootstrap_dir }} {{ grafana_dir }}

# Assumes the state bucket already exists (state lives in it, see backend.tf). A brand-new
# environment bootstraps the bucket first with the two-step in ADR-0005. When neither
# terraform.tfvars nor the environment sets grafana_external_id or operator_email,
# scripts/bootstrap.sh reads each back from AWS: the external ID from the live Grafana
# CloudWatch role, the email from the spend-leak topic's email subscription.
# Apply the state-bootstrap stack once, before the first `just cluster-up`.
[group('cluster')]
bootstrap:
    {{ justfile_directory() }}/scripts/bootstrap.sh {{ bootstrap_dir }}
