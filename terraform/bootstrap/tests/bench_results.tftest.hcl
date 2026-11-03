# Plan-level tests for the bench-results bucket on the bootstrap stack. They run offline
# (mocked aws provider) and assert the bucket's name prefix (the prefix flow-dispatch
# lists by), that a destroy fails on a non-empty bucket rather than deleting run data,
# that only the two chart prefixes are opened for anonymous read, and that the stack
# outputs the name and ARN the recipes and the bench-endpoint stack read.
mock_provider "aws" {
  source = "./tests/mocks"
}
mock_provider "random" {}

# No defaults, so every run supplies the spend-leak topic's email address and the Grafana
# Cloud stack's external ID.
variables {
  operator_email      = "operator@example.com"
  grafana_external_id = "123456"
}

run "bench_results_bucket" {
  command = plan

  # The bucket's name and ARN are computed by AWS; fix them at plan so the bucket policy
  # and outputs are known and assertable exactly.
  override_resource {
    target          = aws_s3_bucket.bench_results
    override_during = plan
    values = {
      id     = "slipstream-bench-results-abc123"
      bucket = "slipstream-bench-results-abc123"
      arn    = "arn:aws:s3:::slipstream-bench-results-abc123"
    }
  }

  # The flow-dispatch workflow finds the bucket by listing this prefix
  # (<cluster_name>-bench-results-), so renaming it breaks the dispatch's lookup.
  assert {
    condition     = aws_s3_bucket.bench_results.bucket_prefix == "slipstream-bench-results-"
    error_message = "Bench-results bucket prefix must be <cluster_name>-bench-results-, the prefix flow-dispatch lists by."
  }

  # Results, the Prefect cache and chart links must survive every teardown, so a destroy
  # of a non-empty bucket fails loudly instead of deleting them.
  assert {
    condition     = aws_s3_bucket.bench_results.force_destroy != true
    error_message = "Bench-results bucket must not be force-destroyed; a destroy must fail on a non-empty bucket."
  }

  # ACLs stay blocked; the two policy flags are off so the scoped bucket policy below can
  # open the chart prefixes (ADR-0018 Amendment).
  assert {
    condition     = aws_s3_bucket_public_access_block.bench_results.block_public_acls && aws_s3_bucket_public_access_block.bench_results.ignore_public_acls && !aws_s3_bucket_public_access_block.bench_results.block_public_policy && !aws_s3_bucket_public_access_block.bench_results.restrict_public_buckets
    error_message = "Bench-results bucket must block public ACLs but allow a scoped public bucket policy for the chart prefixes."
  }

  # The only public grant is anonymous s3:GetObject on the two charts patterns. It must
  # never widen to the bare bucket or the sibling cell-result JSONs.
  assert {
    condition     = length(jsondecode(aws_s3_bucket_policy.bench_results_public_charts.policy).Statement) == 1 && jsondecode(aws_s3_bucket_policy.bench_results_public_charts.policy).Statement[0].Effect == "Allow" && jsondecode(aws_s3_bucket_policy.bench_results_public_charts.policy).Statement[0].Principal == "*" && jsondecode(aws_s3_bucket_policy.bench_results_public_charts.policy).Statement[0].Action == "s3:GetObject" && toset(jsondecode(aws_s3_bucket_policy.bench_results_public_charts.policy).Statement[0].Resource) == toset(["arn:aws:s3:::slipstream-bench-results-abc123/knob-sweep/*/charts/*", "arn:aws:s3:::slipstream-bench-results-abc123/kv-offload-ab/*/charts/*"])
    error_message = "Bench-results bucket policy must grant anonymous s3:GetObject on exactly the knob-sweep/*/charts/* and kv-offload-ab/*/charts/* prefixes, nothing wider."
  }

  assert {
    condition     = output.bench_results_bucket_name == "slipstream-bench-results-abc123"
    error_message = "The bench_results_bucket_name output must carry the bench-results bucket's name."
  }
  assert {
    condition     = output.bench_results_bucket_arn == "arn:aws:s3:::slipstream-bench-results-abc123"
    error_message = "The bench_results_bucket_arn output must carry the bench-results bucket's ARN."
  }
}
