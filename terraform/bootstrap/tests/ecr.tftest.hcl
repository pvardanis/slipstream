# Plan-level tests for the bench-client ECR lifecycle policy on the bootstrap
# stack. They run offline (mocked aws provider) and assert the rule shape: the
# floating -main tag is claimed by a higher-priority rule so the -sha count rule
# never expires it, the count rule keeps the configured number of -sha images, and
# the existing untagged-7-day rule stays.
mock_provider "aws" {}
mock_provider "random" {}

run "bench_client_lifecycle_rules" {
  command = plan

  # Rule 1 matches the floating -main tag at a lower rulePriority number than the
  # count rule; ECR applies rules by ascending priority and cannot expire an image
  # matched by a higher-priority rule, so -main survives however many -sha images
  # accumulate. Guard the whole protection mechanism: pattern, priority order, and
  # that rule 1 keeps (does not expire) the current -main image.
  assert {
    condition     = local.bench_client_lifecycle_rules[0].selection.tagPatternList[0] == "*-main"
    error_message = "The highest-priority rule must match the floating -main tag."
  }
  assert {
    condition     = local.bench_client_lifecycle_rules[0].rulePriority < local.bench_client_lifecycle_rules[1].rulePriority
    error_message = "The -main protection rule must be applied before the -sha count rule."
  }
  assert {
    condition     = local.bench_client_lifecycle_rules[0].selection.countType == "imageCountMoreThan" && local.bench_client_lifecycle_rules[0].selection.countNumber == 1
    error_message = "The -main rule must keep the single newest -main image (imageCountMoreThan 1)."
  }

  # Rule 2 spans every tagged image (the -sha builds, plus -main which rule 1 has
  # already claimed) and keeps the last N, expiring older ones.
  assert {
    condition     = local.bench_client_lifecycle_rules[1].selection.tagPatternList[0] == "*"
    error_message = "The -sha retention rule must span all tagged images."
  }
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

# The retention count is wired from var.bench_image_keep_count, not a hardcoded
# literal; overriding the variable must change the rendered rule.
run "retention_count_is_configurable" {
  command = plan

  variables {
    bench_image_keep_count = 3
  }

  assert {
    condition     = local.bench_client_lifecycle_rules[1].selection.countNumber == 3
    error_message = "The -sha retention count must come from var.bench_image_keep_count."
  }
}
