# The ECR repository holding the bench-client image (slipstream-bench + the vLLM
# tokenizer, baked on the pinned engine build). It lives in the bootstrap stack,
# not eks, because the image must exist before any cluster can pull it and must
# outlive `just down` — a repo in the eks stack would be destroyed on every
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

# Reusing the tag leaves the prior build untagged on each push; expire those so
# ECR storage stays bounded across the duty-cycle.
resource "aws_ecr_lifecycle_policy" "bench_client" {
  repository = aws_ecr_repository.bench_client.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
    ]
  })
}
