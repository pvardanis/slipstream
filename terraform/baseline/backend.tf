# Remote state on S3 with the native lockfile (no DynamoDB).
# Own state key, separate from the eks and bootstrap stacks: bench-endpoint-down
# destroys this stack wholesale without ever touching the long-lived cluster
# state. Partial backend config: the bucket name carries a random suffix from
# the bootstrap stack, so it is supplied at init via -backend-config (see
# justfile). Region and key are fixed literals; region must match the bootstrap
# default.
terraform {
  backend "s3" {
    key          = "baseline/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}
