# Plan-level tests for the bench-client ECR lifecycle policy on the bootstrap
# stack. They run offline (mocked aws provider) and assert the rule shape: the
# floating -main tag is claimed by a higher-priority rule so the -sha count rule
# never expires it, the count rule keeps the configured number of -sha images, and
# the existing untagged-7-day rule stays.
mock_provider "aws" {}
mock_provider "random" {}

run "bench_client_lifecycle_rules" {
  command = plan

  # Rule 1 claims the floating -main tag before the count rule runs (lower priority
  # number is evaluated first), so -main is never expired however many -sha images
  # accumulate.
  assert {
    condition     = local.bench_client_lifecycle_rules[0].selection.tagPatternList[0] == "*-main"
    error_message = "The highest-priority rule must match the floating -main tag."
  }
  assert {
    condition     = local.bench_client_lifecycle_rules[0].rulePriority < local.bench_client_lifecycle_rules[1].rulePriority
    error_message = "The -main protection rule must be evaluated before the -sha count rule."
  }

  # Rule 2 keeps the last N content-tagged images and expires older ones.
  assert {
    condition     = local.bench_client_lifecycle_rules[1].selection.countType == "imageCountMoreThan"
    error_message = "The -sha retention rule must expire by image count."
  }
  assert {
    condition     = local.bench_client_lifecycle_rules[1].selection.countNumber == 10
    error_message = "The -sha retention rule must keep the last 10 images by default."
  }
  assert {
    condition     = local.bench_client_lifecycle_rules[1].action.type == "expire"
    error_message = "The -sha retention rule must expire images beyond the count."
  }

  # The existing untagged-7-day rule stays.
  assert {
    condition     = one([for r in local.bench_client_lifecycle_rules : r if r.selection.tagStatus == "untagged"]).selection.countNumber == 7
    error_message = "Untagged images must still expire after 7 days."
  }
}
