# Remote state on S3 with the native lockfile, in the very bucket this stack
# creates. The bucket name is a literal, not a `-backend-config` lookup like the
# eks stack uses: bootstrap is the root of the state chain and cannot resolve its
# own bucket dynamically without a circular init. The bucket has `prevent_destroy`,
# so this name is durable.
#
# A brand-new environment (bucket does not exist yet) bootstraps in two steps —
# `init -backend=false` → `apply` → `init -migrate-state`; see ADR-0005.
terraform {
  backend "s3" {
    bucket       = "slipstream-tf-state-9e30fb"
    key          = "bootstrap/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}
