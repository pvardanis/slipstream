# Remote state on S3 with the native lockfile (no DynamoDB).
# The backend block takes no variables: bucket and region are literals that
# must match terraform/bootstrap (state_bucket_name default + region default).
terraform {
  backend "s3" {
    bucket       = "slipstream-tf-state"
    key          = "eks/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}
