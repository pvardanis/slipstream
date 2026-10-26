# Inputs for the persistent grafana Terraform stack.
variable "region" {
  description = "AWS region hosting the Terraform state bucket the bootstrap stack's outputs are read from, and the Secrets Manager secret holding the Grafana API token. Must match the bootstrap default."
  type        = string
  default     = "eu-west-1"
}

variable "state_bucket" {
  description = "S3 bucket holding remote state, used to read the bootstrap stack's outputs. Supplied from the bootstrap output at apply time."
  type        = string

  validation {
    condition     = length(var.state_bucket) > 0
    error_message = "state_bucket must be non-empty; a failed bootstrap output lookup would otherwise read state from an empty bucket name."
  }
}

variable "grafana_url" {
  description = "URL of the Grafana Cloud stack, https://<stack>.grafana.net, the one the bench/grafana-cloud-setup.sh walkthrough has the operator create. Override it in the gitignored terraform.tfvars or as TF_VAR_grafana_url to point at another stack."
  type        = string
  default     = "https://slipstreamobs.grafana.net"

  validation {
    condition     = can(regex("^https://[a-z0-9-]+\\.grafana\\.net/?$", var.grafana_url))
    error_message = "grafana_url must be a Grafana Cloud stack URL such as https://<stack>.grafana.net."
  }
}
