# Plan-level tests for the Grafana Cloud data sources the persistent grafana Terraform stack owns
# or reads (ADR-0026). They run offline: the aws and grafana providers are mocked and the
# bootstrap stack's remote state is overridden, so the assertions check how the data
# sources are wired without a Grafana Cloud stack. Grafana actually assuming the role is
# checked by the CloudWatch data source's Save & test after apply.
mock_provider "aws" {}
mock_provider "grafana" {}

# The overridden region and secret name differ from this stack's own defaults, so the
# assertions on them fail if the code reads var.region or a literal instead of the bootstrap
# outputs.
override_data {
  target = data.terraform_remote_state.bootstrap
  values = {
    outputs = {
      region                        = "eu-central-1"
      grafana_cloud_api_secret_name = "test/grafana-api"
      grafana_cloudwatch_role_arn   = "arn:aws:iam::111122223333:role/slipstream-grafana-cloudwatch"
    }
  }
}

# grafana_url is left at its default, so the runs also check the hosted data source names
# the default URL produces.
variables {
  state_bucket = "slipstream-tf-state-test"
}

# Grafana Assume Role: the data source reads CloudWatch through the bootstrap stack's IAM
# role, so no AWS key is stored in Grafana.
run "cloudwatch_reads_through_the_bootstrap_role" {
  command = plan

  assert {
    condition     = grafana_data_source.cloudwatch.type == "cloudwatch"
    error_message = "The CloudWatch data source must be of type cloudwatch."
  }
  assert {
    condition     = jsondecode(grafana_data_source.cloudwatch.json_data_encoded).authType == "grafana_assume_role"
    error_message = "The CloudWatch data source must authenticate with Grafana Assume Role."
  }
  assert {
    condition     = jsondecode(grafana_data_source.cloudwatch.json_data_encoded).assumeRoleArn == "arn:aws:iam::111122223333:role/slipstream-grafana-cloudwatch"
    error_message = "The CloudWatch data source must assume the bootstrap stack's grafana_cloudwatch_role_arn."
  }
  assert {
    condition     = jsondecode(grafana_data_source.cloudwatch.json_data_encoded).defaultRegion == "eu-central-1"
    error_message = "The CloudWatch data source's default region must be the bootstrap stack's region."
  }
}

# Grafana Cloud provisions the hosted Prometheus and Tempo data sources with every stack,
# named after its slug, so this Terraform stack reads them by name instead of managing copies that
# would need a read token of their own.
run "hosted_prometheus_and_tempo_are_read_by_stack_name" {
  command = plan

  assert {
    condition     = data.grafana_data_source.prometheus.name == "grafanacloud-slipstreamobs-prom"
    error_message = "The hosted Prometheus data source must be read as grafanacloud-<stack>-prom."
  }
  assert {
    condition     = data.grafana_data_source.tempo.name == "grafanacloud-slipstreamobs-traces"
    error_message = "The hosted Tempo data source must be read as grafanacloud-<stack>-traces."
  }
}

# The Grafana provider authenticates with the service account token the setup wizard
# writes into the bootstrap stack's grafana-cloud/api secret.
run "provider_token_is_read_from_the_bootstrap_api_secret" {
  command = plan

  assert {
    condition     = data.aws_secretsmanager_secret_version.grafana_api.secret_id == "test/grafana-api"
    error_message = "The Grafana provider's token must be read from the bootstrap stack's grafana_cloud_api_secret_name."
  }
}

# The hosted data sources' names come from the stack URL's slug, so anything but a
# https://<stack>.grafana.net URL is rejected by the variable rather than failing inside
# the slug lookup.
run "grafana_url_must_be_a_stack_url" {
  command = plan

  variables {
    grafana_url = "https://grafana.example.com"
  }

  expect_failures = [var.grafana_url]
}

# A trailing slash, as a browser's address bar often shows, still names the same stack.
run "grafana_url_may_end_in_a_slash" {
  command = plan

  variables {
    grafana_url = "https://slipstreamobs.grafana.net/"
  }

  assert {
    condition     = data.grafana_data_source.prometheus.name == "grafanacloud-slipstreamobs-prom"
    error_message = "A stack URL with a trailing slash must name the same hosted data sources."
  }
}

# An empty bucket name, from a failed bootstrap output lookup, is rejected by the variable
# rather than reaching the bootstrap remote state read.
run "state_bucket_must_be_non_empty" {
  command = plan

  variables {
    state_bucket = ""
  }

  expect_failures = [var.state_bucket]
}

# The stack URL is published for the Grafana MCP server launcher (scripts/grafana-mcp.sh),
# which reads it from this stack's state in a worktree whose environment and tfvars set no
# URL. The URL differs from var.grafana_url's default, so the assert fails if the output
# hardcodes the default instead of reading the variable.
run "grafana_url_is_published_for_the_mcp_server" {
  command = plan

  variables {
    grafana_url = "https://otherstack.grafana.net"
  }

  assert {
    condition     = output.grafana_url == "https://otherstack.grafana.net"
    error_message = "The grafana_url output must be the stack URL the provider uses."
  }
}
