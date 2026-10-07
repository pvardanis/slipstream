# The results bucket the bench host writes measurement JSON to. It lives outside
# the ephemeral host so a run's numbers survive the host's teardown: the host is
# destroyed on bench-endpoint-down, but its results stay downloadable from the S3
# console until bench-endpoint-down removes this bucket too. force_destroy lets that
# teardown delete the bucket even with objects still in it, rather than failing.

resource "aws_s3_bucket" "results" {
  bucket_prefix = "${local.name}-results-"
  force_destroy = true
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket = aws_s3_bucket.results.id

  # ACLs stay blocked — public access comes only from the scoped bucket policy below, never
  # object ACLs. The two policy flags are off so that policy can open one prefix: the render
  # task serves its chart PNGs as public image artifacts on the Prefect run page (ADR-0018
  # Amendment). block_public_policy off lets the Principal:"*" policy attach;
  # restrict_public_buckets off lets anonymous GETs through to it.
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = false
}

# Opens exactly the chart prefix for anonymous read, so the render task's plot PNGs load as
# public image artifacts in the Prefect UI (inline, zoomable, never expiring — ADR-0018
# Amendment). Scoped to sweeps/<run>/charts/* and nothing wider: the sibling cell-result JSONs
# under sweeps/<run>/<point>/ hold the measurements and stay private. depends_on the public
# access block so block_public_policy is already off when PutBucketPolicy runs.
resource "aws_s3_bucket_policy" "results_public_charts" {
  bucket = aws_s3_bucket.results.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadCharts"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.results.arn}/sweeps/*/charts/*"
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.results]
}
