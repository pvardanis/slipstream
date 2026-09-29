# The Prefect Cloud address, published to Secrets Manager so the EKS worker and CI
# read the API URL and key from one secret rather than a committed file or a command
# line, where the key would show in process args and shell history. The URL and key
# travel together as one JSON secret keyed to the env vars the Prefect client reads
# (PREFECT_API_URL, PREFECT_API_KEY). recovery_window_in_days = 0 lets prefect-cloud-down
# delete it immediately instead of leaving a 30-day tombstone that blocks re-creation.

resource "aws_secretsmanager_secret" "prefect_api" {
  name_prefix             = "slipstream-prefect-api-"
  recovery_window_in_days = 0
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "prefect_api" {
  secret_id = aws_secretsmanager_secret.prefect_api.id
  secret_string = jsonencode({
    PREFECT_API_URL = local.prefect_api_url
    PREFECT_API_KEY = var.prefect_api_key
  })
}
