# Task runner for the slipstream platform. `just up` conjures the cluster;
# `just down` returns spend to zero. Credentials come from your AWS_PROFILE.
# Run `just` with no args to list recipes.

eks_dir := "terraform/eks"
bootstrap_dir := "terraform/bootstrap"

# List available recipes.
default:
    @just --list

# Create the cluster and point kubectl at it.
up:
    terraform -chdir={{ eks_dir }} init
    terraform -chdir={{ eks_dir }} apply -auto-approve
    aws eks update-kubeconfig \
      --name $(terraform -chdir={{ eks_dir }} output -raw cluster_name) \
      --region $(terraform -chdir={{ eks_dir }} output -raw region)
    kubectl get nodes

# Destroy the cluster (the bootstrap state bucket is left intact).
down:
    terraform -chdir={{ eks_dir }} destroy -auto-approve

# Apply the state-bootstrap stack once, before the first `just up`.
bootstrap:
    terraform -chdir={{ bootstrap_dir }} init
    terraform -chdir={{ bootstrap_dir }} apply -auto-approve
