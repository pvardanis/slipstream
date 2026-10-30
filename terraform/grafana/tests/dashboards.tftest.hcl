# Plan-level tests for the dashboards the persistent grafana Terraform stack applies from
# the JSON files in dashboards/ (ADR-0026, "Dashboards as code"). They run offline with the
# same mocks as data_sources.tftest.hcl: the aws and grafana providers are mocked and the
# bootstrap stack's remote state is overridden. What the panels show is checked after
# apply, in Grafana.
mock_provider "aws" {}
# URL-shaped send endpoints and set instance IDs, so the outputs' preconditions pass.
mock_provider "grafana" {
  mock_data "grafana_cloud_stack" {
    defaults = {
      prometheus_remote_write_endpoint = "https://prometheus-mock.grafana.net/api/prom/push"
      prometheus_user_id               = 1111111
      traces_url                       = "https://tempo-mock.grafana.net"
      traces_user_id                   = 2222222
    }
  }
}

override_data {
  target = data.terraform_remote_state.bootstrap
  values = {
    outputs = {
      region                               = "eu-central-1"
      grafana_cloud_api_secret_name        = "test/grafana-api"
      grafana_cloud_stack_read_secret_name = "test/grafana-stack-read"
      grafana_cloudwatch_role_arn          = "arn:aws:iam::111122223333:role/slipstream-grafana-cloudwatch"
    }
  }
}

variables {
  state_bucket = "slipstream-tf-state-test"
}

# A dashboard is created, updated or deleted when its file is: one dashboard resource per
# JSON file, keyed by file name, so removing a file destroys only its dashboard.
run "one_dashboard_per_dashboard_file" {
  command = plan

  assert {
    condition     = toset(keys(grafana_dashboard.this)) == fileset("${path.module}/dashboards", "*.json")
    error_message = "There must be exactly one dashboard resource per dashboards/*.json file, keyed by file name."
  }
  assert {
    condition     = contains(keys(grafana_dashboard.this), "fleet-health.json")
    error_message = "The fleet health dashboard must be applied from dashboards/fleet-health.json."
  }
}

# Panels name their data source as ${DS_PROMETHEUS}, ${DS_TEMPO} or ${DS_CLOUDWATCH}, which
# the stack replaces with the hosted Prometheus or Tempo data source's UID or the managed
# CloudWatch data source's; overriding those UIDs shows the panels take them from there,
# not from a literal.
run "panels_query_the_hosted_data_sources_by_uid" {
  command = plan

  override_data {
    target = data.grafana_data_source.prometheus
    values = {
      uid = "prom-uid-test"
    }
  }
  override_data {
    target = data.grafana_data_source.tempo
    values = {
      uid = "tempo-uid-test"
    }
  }
  override_resource {
    target          = grafana_data_source.cloudwatch
    override_during = plan
    values = {
      uid = "cloudwatch-uid-test"
    }
  }

  assert {
    condition = alltrue(flatten([
      for d in values(grafana_dashboard.this) : [
        for placeholder in ["DS_PROMETHEUS", "DS_TEMPO", "DS_CLOUDWATCH"] :
        !strcontains(d.config_json, placeholder)
      ]
    ]))
    error_message = "Every $${DS_PROMETHEUS}, $${DS_TEMPO} and $${DS_CLOUDWATCH} placeholder must be replaced with its data source's UID."
  }
  # Every panel that runs queries, including those nested in a collapsed row, and each of
  # its queries; a row itself runs none. A query reads the data source its type names.
  assert {
    condition = alltrue(flatten([
      for d in values(grafana_dashboard.this) : [
        for p in flatten([for top in jsondecode(d.config_json).panels : concat([top], try(top.panels, []))]) : [
          for q in concat([p], p.targets) :
          q.datasource.uid == lookup({ tempo = "tempo-uid-test", cloudwatch = "cloudwatch-uid-test" }, q.datasource.type, "prom-uid-test")
        ] if length(try(p.targets, [])) > 0
      ]
    ]))
    error_message = "Every dashboard panel and query must read the hosted Prometheus or Tempo data source, or the managed CloudWatch data source, whichever its type names, by its UID."
  }
  # The requests-over-the-TTFT-gate table searches traces, so fleet health reads Tempo as well.
  assert {
    condition = anytrue([
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels :
      try(p.datasource.uid, "") == "tempo-uid-test"
    ])
    error_message = "The fleet health dashboard must have a panel that reads the hosted Tempo data source."
  }
  # The spend panel reads AWS/Usage, which only CloudWatch holds, so fleet health reads it
  # through the managed CloudWatch data source.
  assert {
    condition = anytrue([
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels :
      try(p.datasource.uid, "") == "cloudwatch-uid-test"
    ])
    error_message = "The fleet health dashboard must have a panel that reads the managed CloudWatch data source."
  }
}

# AWS/Usage publishes one datapoint every 5 minutes, so a shorter period leaves empty
# minutes that FILL draws as 0 between the real counts.
run "cloudwatch_queries_use_the_aws_usage_period" {
  command = plan

  override_resource {
    target          = grafana_data_source.cloudwatch
    override_during = plan
    values = {
      uid = "cloudwatch-uid-test"
    }
  }

  assert {
    condition = alltrue(flatten([
      for d in values(grafana_dashboard.this) : [
        for p in flatten([for top in jsondecode(d.config_json).panels : concat([top], try(top.panels, []))]) : [
          for q in try(p.targets, []) : q.period == "300" if q.datasource.type == "cloudwatch"
        ]
      ]
    ]))
    error_message = "Every CloudWatch query must use a 300 s period, the interval AWS/Usage publishes at."
  }
}
