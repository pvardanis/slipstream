# Remote state on S3 with the native lockfile (no DynamoDB), its own key separate
# from the eks, bootstrap and bench-endpoint stacks. State holds the Prefect API
# key in plaintext (Terraform writes every argument to state), so it must live in
# the bootstrap bucket, which is versioned, SSE-encrypted and public-access-blocked.
# Partial backend config: the bucket name carries a random suffix from the bootstrap
# stack, so it is supplied at init via -backend-config (see justfile). Region and
# key are fixed literals; region must match the bootstrap default.
terraform {
  backend "s3" {
    key          = "prefect-cloud/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}
