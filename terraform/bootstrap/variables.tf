# Inputs for the state-bootstrap stack.
variable "region" {
  description = "AWS region hosting the Terraform state bucket and the stacks the bootstrap roles reach (the sweep-dispatch role's cluster ARN and DescribeInstances region condition)."
  type        = string
  default     = "eu-west-1"
}

variable "state_bucket_prefix" {
  description = "Prefix for the remote-state bucket; a random suffix is appended for global uniqueness."
  type        = string
  default     = "slipstream-tf-state"
}

variable "bench_image_repo_name" {
  description = "Name of the ECR repository holding the bench-client image."
  type        = string
  default     = "slipstream/bench-client"
}

variable "bench_image_keep_count" {
  description = "Number of most-recent content-tagged (-sha) bench-client images the ECR lifecycle policy retains before expiring older ones."
  type        = number
  default     = 10
}

variable "orchestration_image_repo_name" {
  description = "Name of the ECR repository holding the orchestration image (the baked Prefect-worker image that drives the knob sweep)."
  type        = string
  default     = "slipstream/orchestration"
}

variable "orchestration_image_keep_count" {
  description = "Number of most-recent content-tagged (-sha) orchestration images the ECR lifecycle policy retains before expiring older ones."
  type        = number
  default     = 10
}

variable "github_oidc_sub_prefix" {
  description = "Immutable OIDC subject prefix for the GitHub repository whose Actions workflows may assume the bench-client push role, in the form repo:<owner>@<owner_id>/<repo>@<repo_id>. Read once from the repo's OIDC sub-claim customization (gh api repos/OWNER/REPO/actions/oidc/customization/sub); the numeric IDs are immutable, so the trust survives owner or repo renames."
  type        = string
  default     = "repo:pvardanis@37624791/slipstream@1357264197"
}

variable "bench_image_push_role_name" {
  description = "Name of the IAM role GitHub Actions assumes via OIDC to push the bench-client image."
  type        = string
  default     = "slipstream-bench-image-push"
}

variable "orchestration_image_push_role_name" {
  description = "Name of the IAM role GitHub Actions assumes via OIDC to push the orchestration image."
  type        = string
  default     = "slipstream-orchestration-image-push"
}

variable "sweep_dispatch_role_name" {
  description = "Name of the IAM role the flow-dispatch GitHub Actions workflow (behind sweep-dispatch and kv-ab-dispatch) assumes via OIDC to reach the cluster and trigger the knob sweep or the KV cache A/B."
  type        = string
  default     = "slipstream-sweep-dispatch"
}

variable "cluster_name" {
  description = "Name of the EKS cluster the sweep-dispatch role may describe, also published as the cluster_name output gh-vars sets as EKS_CLUSTER_NAME. Must match the eks stack's cluster_name (both default to slipstream); the eks plan fails if they differ."
  type        = string
  default     = "slipstream"
}

variable "operator_email" {
  description = "Email address the spend-leak SNS topic pages (the GPU alarm and the monthly budget). No default, so the address stays out of this public repo: set it in the gitignored terraform.tfvars or as TF_VAR_operator_email. AWS emails a confirmation link on apply that must be clicked before any page is delivered."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.operator_email))
    error_message = "operator_email must be an email address such as name@example.com."
  }
}

variable "prefect_namespace" {
  description = "Namespace the Prefect server runs in, which the flow-dispatch workflow port-forwards into. Must match the eks stack's prefect_namespace local (prefect)."
  type        = string
  default     = "prefect"
}

variable "grafana_external_id" {
  description = "External ID Grafana Cloud presents when assuming the Grafana Assume Role IAM role, unique to the Grafana Cloud stack. Shown in the instructions box under Connection Details of the stack's CloudWatch data source once Grafana Assume Role is selected. No default because it exists only once that stack does: bench/grafana-cloud-setup.sh walks the operator to it and writes it into the gitignored terraform.tfvars; TF_VAR_grafana_external_id also sets it."
  type        = string

  validation {
    condition     = length(trimspace(var.grafana_external_id)) > 0
    error_message = "grafana_external_id must be the external ID shown under Connection Details of the Grafana Cloud CloudWatch data source."
  }
}

variable "grafana_account_id" {
  description = "Grafana Cloud's shared AWS account, the principal the Grafana Assume Role IAM role trusts. The instructions box under Connection Details of the CloudWatch data source shows it once Grafana Assume Role is selected; override it only if that box shows another account."
  type        = string
  default     = "008923505280"

  validation {
    condition     = can(regex("^[0-9]{12}$", var.grafana_account_id))
    error_message = "grafana_account_id must be a 12-digit AWS account ID."
  }
}

variable "grafana_cloudwatch_role_name" {
  description = "Name of the IAM role Grafana Cloud's CloudWatch data source assumes (Grafana Assume Role)."
  type        = string
  default     = "slipstream-grafana-cloudwatch"
}
