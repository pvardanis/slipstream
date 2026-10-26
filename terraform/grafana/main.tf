# The persistent grafana Terraform stack (ADR-0026): the Grafana provider and the data
# sources the dashboards query. It outlives `just cluster-down`, like the history it shows.

# Read-only reference to the bootstrap stack's outputs: the IAM role the CloudWatch data
# source assumes and the region it reads. Both live in bootstrap because they must exist
# before Grafana Cloud is set up.
data "terraform_remote_state" "bootstrap" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "bootstrap/terraform.tfstate"
    region = var.region
  }
}

# The service account token the setup wizard writes into the bootstrap stack's
# grafana-cloud/api secret. Read through a data source, it lands in this stack's state,
# the usual price of a provider credential (ADR-0026); the state bucket is encrypted.
data "aws_secretsmanager_secret_version" "grafana_api" {
  secret_id = data.terraform_remote_state.bootstrap.outputs.grafana_cloud_api_secret_name
}

provider "aws" {
  region = var.region
}

provider "grafana" {
  url  = var.grafana_url
  auth = data.aws_secretsmanager_secret_version.grafana_api.secret_string
}
