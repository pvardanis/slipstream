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

# Panels name their data source as ${DS_PROMETHEUS}, which the stack replaces with the
# hosted Prometheus data source's UID; the mocked UID shows it is the one read by name.
run "panels_query_the_hosted_prometheus_by_uid" {
  command = plan

  override_data {
    target = data.grafana_data_source.prometheus
    values = {
      uid = "prom-uid-test"
    }
  }

  assert {
    condition = alltrue([
      for d in values(grafana_dashboard.this) : !strcontains(d.config_json, "DS_PROMETHEUS")
    ])
    error_message = "Every $${DS_PROMETHEUS} placeholder must be replaced with the hosted Prometheus data source's UID."
  }
  assert {
    condition = alltrue(flatten([
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels : [
        for t in concat([p], p.targets) : t.datasource.uid == "prom-uid-test"
      ]
    ]))
    error_message = "Every fleet health panel and query must read the hosted Prometheus data source by its UID."
  }
}
