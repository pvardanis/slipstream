# The ECR repository holding the bench-client image (slipstream-bench + the vLLM
# tokenizer, baked on the pinned engine build). It lives in the bootstrap stack,
# not eks, because the image must exist before any cluster can pull it and must
# outlive `just cluster-down` — a repo in the eks stack would be destroyed on every
# teardown, orphaning the pushed image. EKS nodes pull from it via the ECR pull
# policy the managed node group attaches to their node IAM role, so no
# imagePullSecrets are needed.
resource "aws_ecr_repository" "bench_client" {
  name = var.bench_image_repo_name

  # Rebuilt in place during the dev loop and pulled with imagePullPolicy: Always,
  # so the tag is reused rather than pinned; production serving would pin by digest.
  image_tag_mutability = "MUTABLE"

  # Surface CVEs in the baked layers on every push.
  image_scanning_configuration {
    scan_on_push = true
  }
}

# The lifecycle keeps ECR storage bounded across the duty-cycle. Rules are
# evaluated in ascending priority order and an image an earlier rule selects is not
# re-evaluated by later ones, so rule 1 claims the floating -main tag first and the
# -sha count rule (rule 2) can never expire it. Rule 3 sweeps the images a tag move
# or replaced push leaves untagged.
resource "aws_ecr_lifecycle_policy" "bench_client" {
  repository = aws_ecr_repository.bench_client.name

  policy = jsonencode({ rules = local.bench_client_lifecycle_rules })
}

locals {
  bench_client_lifecycle_rules = [
    {
      rulePriority = 1
      description  = "Keep the floating -main tag; claimed before the -sha count rule so it is never expired."
      selection = {
        tagStatus      = "tagged"
        tagPatternList = ["*-main"]
        countType      = "imageCountMoreThan"
        countNumber    = 1
      }
      action = { type = "expire" }
    },
    {
      # The count spans all tagged images, but the build tags each -main manifest
      # with its -sha too, so -main never occupies a slot a -sha would not: the
      # kept set is N distinct content builds. Priority claims aside, an image an
      # earlier rule selected still counts toward this rule's total (ECR semantics).
      rulePriority = 2
      description  = "Keep the last ${var.bench_image_keep_count} content-tagged (-sha) images; expire older ones."
      selection = {
        tagStatus      = "tagged"
        tagPatternList = ["*"]
        countType      = "imageCountMoreThan"
        countNumber    = var.bench_image_keep_count
      }
      action = { type = "expire" }
    },
    {
      rulePriority = 3
      description  = "Expire untagged images after 7 days."
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 7
      }
      action = { type = "expire" }
    },
  ]
}
