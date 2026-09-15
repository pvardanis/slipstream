# The results bucket the bench host writes measurement JSON to. It lives outside
# the ephemeral host so a run's numbers survive the host's teardown: the host is
# destroyed on baseline-down, but its results stay downloadable from the S3
# console until baseline-down removes this bucket too. force_destroy lets that
# teardown delete the bucket even with objects still in it, rather than failing.

resource "aws_s3_bucket" "results" {
  bucket_prefix = "${local.name}-results-"
  force_destroy = true
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket = aws_s3_bucket.results.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
