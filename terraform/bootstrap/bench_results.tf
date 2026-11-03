# The bench-results bucket: measurement JSON from the bench host, the Prefect result
# and cache-key store the resume cache reads (ADR-0022), and the chart PNGs the render
# task links from the Prefect UI. It lives in the bootstrap stack, not bench-endpoint,
# so results, the cache and chart links survive bench-endpoint-down and stack-down, and
# registering a deployment needs no live bench endpoint (ADR-0024 Amendment). It has
# neither force_destroy nor prevent_destroy: a bootstrap destroy fails loudly on a
# non-empty bucket instead of deleting run data, and succeeds once it is emptied.

resource "aws_s3_bucket" "bench_results" {
  bucket_prefix = "${var.cluster_name}-bench-results-"
}

resource "aws_s3_bucket_public_access_block" "bench_results" {
  bucket = aws_s3_bucket.bench_results.id

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

# Opens the chart paths for anonymous read, so the render task's plot PNGs load as public
# image artifacts in the Prefect UI (inline, zoomable, never expiring — ADR-0018 Amendment).
# Scoped to knob-sweep/*/charts/* and kv-offload-ab/*/charts/*; the * spans "/", so each
# matches any charts/ path under its experiment's prefix — the knob sweep's
# knob-sweep/<run>/charts/ and the kv-ab flow's kv-offload-ab/<run>/charts/. The
# cell-result JSONs under knob-sweep/<run>/<point>/ and kv-offload-ab/<run>/<arm>-r<n>/
# hold the measurements and stay private only while no run id has a charts segment.
# depends_on the public access block so block_public_policy is already off when
# PutBucketPolicy runs.
resource "aws_s3_bucket_policy" "bench_results_public_charts" {
  bucket = aws_s3_bucket.bench_results.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadCharts"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource = [
          "${aws_s3_bucket.bench_results.arn}/knob-sweep/*/charts/*",
          "${aws_s3_bucket.bench_results.arn}/kv-offload-ab/*/charts/*",
        ]
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.bench_results]
}
