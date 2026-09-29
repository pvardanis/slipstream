# Inputs for the Prefect Cloud stack.
#
# The Hobby (free) tier gives one auto-created workspace and no service accounts
# (those start at the paid Starter tier), so the operator's account and its first
# workspace are a manual, one-time bootstrap: sign up at app.prefect.cloud (no card),
# note the account id and the workspace handle, and mint a personal API key. Those
# three cross into this stack as variables; the stack then references the existing
# workspace, creates the work pool in it, and publishes the API URL + key to
# Secrets Manager. See the prefect-cloud-up recipe for the bootstrap steps.

variable "region" {
  description = "AWS region the Secrets Manager secret is created in. Must match the bootstrap default so the worker and CI find it where they look."
  type        = string
  default     = "eu-west-1"
}

variable "prefect_account_id" {
  description = "Prefect Cloud account id (UUID) from the account URL. Selects the account the workspace and work pool live under, and is embedded in the published API URL."
  type        = string

  validation {
    condition     = length(var.prefect_account_id) > 0
    error_message = "prefect_account_id must be non-empty; the provider and the API URL both need the account to address the workspace."
  }
}

variable "prefect_api_key" {
  description = "Personal Prefect Cloud API key. Authenticates the provider and is published to Secrets Manager for the worker and CI. Passed through the environment (TF_VAR_prefect_api_key), never a committed file, so it does not land in the terraform argv or shell history."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.prefect_api_key) > 0
    error_message = "prefect_api_key must be non-empty; publishing an empty key would authenticate nothing for the worker or CI."
  }
}

variable "workspace_id" {
  description = "Id (UUID) of the existing Hobby workspace, taken from the workspace URL. The stack references this workspace rather than creating one, since Hobby caps the account at a single workspace. The prefect-cloud-up recipe reads it from the active `prefect cloud login` profile."
  type        = string

  validation {
    condition     = length(var.workspace_id) > 0
    error_message = "workspace_id must be non-empty; the workspace lookup needs an id to resolve the workspace."
  }
}

variable "work_pool_name" {
  description = "Name of the Kubernetes work pool the EKS worker polls for sweep runs."
  type        = string
  default     = "eks-sweep-pool"
}
