# Remote state on S3 with the native lockfile (no DynamoDB).
# Own state key, separate from every other stack: this stack outlives `just cluster-down`
# like the dashboards and history it owns (ADR-0026). Partial backend config: the bucket
# name carries a random suffix from the bootstrap stack, so it is supplied at init via
# -backend-config (see `just grafana-up`). Region and key are fixed literals; region must
# match the bootstrap default.
terraform {
  backend "s3" {
    key          = "grafana/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}
