# Remote state on S3 with the native lockfile (no DynamoDB).
# Partial backend config: the bucket name carries a random suffix from the
# bootstrap stack, so it is supplied at init via -backend-config (see justfile).
# Region and key are fixed literals; region must match the bootstrap default.
terraform {
  backend "s3" {
    key          = "eks/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}
