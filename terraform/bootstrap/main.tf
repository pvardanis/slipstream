# One-time, durable infrastructure that must exist before the eks stack and
# outlive `just cluster-down`: the S3 bucket holding remote state, and the ECR repository
# for the bench-client image (see ecr.tf). This stack keeps its own state in that
# bucket too (see backend.tf); the native S3 lockfile means no DynamoDB table is
# needed. Credentials come from the caller's AWS_PROFILE.
provider "aws" {
  region = var.region
}

# A random suffix keeps the bucket name unique in S3's global namespace.
resource "random_id" "suffix" {
  byte_length = 3
}

resource "aws_s3_bucket" "state" {
  bucket = "${var.state_bucket_prefix}-${random_id.suffix.hex}"

  # The state store must survive `just cluster-down`; destroying it orphans all managed state.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
