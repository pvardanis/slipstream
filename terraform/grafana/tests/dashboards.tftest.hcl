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

# A stat panel's figure is read as it is: Grafana's default thresholds would turn it red
# above 80 whatever its unit, and the short unit would round 1,075 to 1K.
run "stat_panels_show_their_figure_in_full_and_uncoloured" {
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
        for p in jsondecode(d.config_json).panels :
        try(p.fieldConfig.defaults.color.mode, "") == "fixed" if p.type == "stat"
      ]
    ]))
    error_message = "Every stat panel must set a fixed colour, not Grafana's default thresholds."
  }
  assert {
    condition = alltrue(flatten([
      for d in values(grafana_dashboard.this) : [
        for p in jsondecode(d.config_json).panels :
        try(p.fieldConfig.defaults.unit, "short") != "short" if p.type == "stat"
      ]
    ]))
    error_message = "No stat panel may use the short unit, which rounds its figure to thousands."
  }
}

# The top row reads Replicas beside the vLLM pods' CPU, both half width, directly above the
# GPU panels, so CPU is read against GPU busy time (ADR-0026, the #436 amendment).
run "fleet_health_top_row_is_replicas_beside_vllm_pod_cpu_above_the_gpu_panels" {
  command = plan

  override_resource {
    target          = grafana_data_source.cloudwatch
    override_during = plan
    values = {
      uid = "cloudwatch-uid-test"
    }
  }

  assert {
    condition = {
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels :
      p.title => p.gridPos if p.gridPos.y == 0
      } == {
      "Replicas"             = { x = 0, y = 0, w = 12, h = 8 }
      "vLLM pod CPU (cores)" = { x = 12, y = 0, w = 12, h = 8 }
    }
    error_message = "The fleet health dashboard's top row must be Replicas and vLLM pod CPU (cores), half width each."
  }
  assert {
    condition = {
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels :
      p.title => p.gridPos if p.gridPos.y == 8
      } == {
      "GPU busy time"               = { x = 0, y = 8, w = 12, h = 8 }
      "GPU memory-bandwidth active" = { x = 12, y = 8, w = 12, h = 8 }
    }
    error_message = "The GPU panels must sit directly below the top row, half width each."
  }
}

# The CPU panel's request line is the vllm-gpu container's CPU request, so it moves with
# the manifest.
run "vllm_pod_cpu_request_line_is_the_manifest_request" {
  command = plan

  override_resource {
    target          = grafana_data_source.cloudwatch
    override_during = plan
    values = {
      uid = "cloudwatch-uid-test"
    }
  }

  assert {
    condition = [
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels : [
        for q in p.targets : q.expr if q.legendFormat == "request"
      ] if p.title == "vLLM pod CPU (cores)"
    ] == [["vector(${regex("requests:\\s+(?:#[^\\n]*\\n\\s*)*cpu:\\s+\"([0-9.]+)\"", file("../../k8s/vllm-gpu.yaml"))[0]})"]]
    error_message = "The vLLM pod CPU panel's request line must be vector(<cpu request>), the vllm-gpu container's CPU request in k8s/vllm-gpu.yaml."
  }
}

# The CPU panel's usage lines select the vllm-gpu Deployment's namespace and container, so a
# rename in the manifest fails here rather than leaving only the reference lines drawn.
run "vllm_pod_cpu_usage_lines_select_the_manifest_namespace_and_container" {
  command = plan

  override_resource {
    target          = grafana_data_source.cloudwatch
    override_during = plan
    values = {
      uid = "cloudwatch-uid-test"
    }
  }

  assert {
    condition = [
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels : [
        for q in p.targets : strcontains(q.expr, format(
          "{namespace=\"%s\", pod=~\"vllm-gpu-.*\", container=\"%s\"}",
          regex("kind: Deployment\\nmetadata:\\n  name: vllm-gpu\\n  namespace: (\\S+)", file("../../k8s/vllm-gpu.yaml"))[0],
          regex("containers:\\n\\s+- name: (\\S+)", file("../../k8s/vllm-gpu.yaml"))[0],
        )) if strcontains(q.expr, "container_cpu_usage_seconds_total")
      ] if p.title == "vLLM pod CPU (cores)"
    ] == [[true, true]]
    error_message = "The vLLM pod CPU panel's fleet and per-pod lines must select the vllm-gpu Deployment's namespace and container from k8s/vllm-gpu.yaml."
  }
}

# The throughput panels pair up half width: the two token rates in one row, requests/s
# beside live goodput in the next.
run "fleet_health_throughput_rows_pair_token_rates_and_requests_with_goodput" {
  command = plan

  override_resource {
    target          = grafana_data_source.cloudwatch
    override_during = plan
    values = {
      uid = "cloudwatch-uid-test"
    }
  }

  assert {
    condition = {
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels :
      p.title => p.gridPos if contains([32, 40], p.gridPos.y)
      } == {
      "Output tokens/s" = { x = 0, y = 32, w = 12, h = 8 }
      "Prompt tokens/s" = { x = 12, y = 32, w = 12, h = 8 }
      "Requests/s"      = { x = 0, y = 40, w = 12, h = 8 }
      "Live goodput"    = { x = 12, y = 40, w = 12, h = 8 }
    }
    error_message = "Output and prompt tokens/s must share one row, and requests/s and live goodput the next, half width each."
  }
}

# The Karpenter rows pair what capacity is up, nodes beside GPU vCPUs running, and node
# churn, voluntary disruptions beside pod-provisioning cold start.
run "fleet_health_karpenter_rows_pair_nodes_with_spend_and_disruptions_with_cold_start" {
  command = plan

  override_resource {
    target          = grafana_data_source.cloudwatch
    override_during = plan
    values = {
      uid = "cloudwatch-uid-test"
    }
  }

  assert {
    condition = {
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels :
      p.title => p.gridPos if contains([56, 64], p.gridPos.y)
      } == {
      "Nodes"                           = { x = 0, y = 56, w = 12, h = 8 }
      "GPU vCPUs running (spend)"       = { x = 12, y = 56, w = 12, h = 8 }
      "Voluntary disruptions by reason" = { x = 0, y = 64, w = 12, h = 8 }
      "Pod-provisioning cold start"     = { x = 12, y = 64, w = 12, h = 8 }
    }
    error_message = "Nodes and GPU vCPUs running must share one row, and voluntary disruptions and pod-provisioning cold start the next, half width each."
  }
}

# The Nodes panel plots each step's lowest node count, over a window no shorter than the
# 30 s scrape interval, so a node gap shorter than the step still shows as a dip.
run "nodes_panel_shows_the_lowest_count_in_each_step" {
  command = plan

  override_resource {
    target          = grafana_data_source.cloudwatch
    override_during = plan
    values = {
      uid = "cloudwatch-uid-test"
    }
  }

  assert {
    condition = [
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels :
      [try(p.interval, null), [for t in p.targets : t.expr]] if p.title == "Nodes"
      ] == [
      ["30s", ["max(min_over_time(karpenter_cluster_state_node_count[$__interval]))"]]
    ]
    error_message = "The Nodes panel must plot min_over_time of karpenter_cluster_state_node_count over $__interval, with a 30s minimum interval."
  }
}

# Each disruption reason is a figure named by its reason. A stat hides the name of a lone
# value unless the text mode asks for it, and a range often holds one reason; the legend
# names the figure, so without it the stat shows the series' labels, not the reason.
run "disruptions_panel_names_each_reason" {
  command = plan

  override_resource {
    target          = grafana_data_source.cloudwatch
    override_during = plan
    values = {
      uid = "cloudwatch-uid-test"
    }
  }

  assert {
    condition = [
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels :
      [p.type, try(p.options.textMode, null), [for t in p.targets : try(t.legendFormat, null)]]
      if p.title == "Voluntary disruptions by reason"
    ] == [["stat", "value_and_name", ["{{reason}}"]]]
    error_message = "The voluntary disruptions panel must be a stat with textMode value_and_name and legend {{reason}}, so each reason's count is named by its reason."
  }
}

# The TTFT table's query fixes the span's service and name, so their columns hold one value
# and are dropped. Tempo's trace service and name columns describe the trace's root span,
# which is the llm_request span while no client sends a traceparent, so they are dropped
# too. Tempo's trace ID stays in the data, as the Span ID link opens the trace through it,
# but is hidden from the table.
run "ttft_table_shows_only_the_columns_that_vary" {
  command = plan

  override_resource {
    target          = grafana_data_source.cloudwatch
    override_during = plan
    values = {
      uid = "cloudwatch-uid-test"
    }
  }

  assert {
    condition = [
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels :
      [for t in try(p.transformations, []) : t.options.excludeByName if t.id == "organize"]
      if p.title == "Requests over the TTFT gate"
      ] == [
      [{ traceService = true, traceName = true, name = true, "service.name" = true }]
    ]
    error_message = "The TTFT table must drop traceService, traceName, name and service.name with one organize transformation, and keep traceIdHidden, which the Span ID link reads."
  }
  assert {
    condition = anytrue(flatten([
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels : [
        for o in p.fieldConfig.overrides :
        o.matcher == { id = "byName", options = "traceIdHidden" } && contains(o.properties, { id = "custom.hideFrom.viz", value = true })
      ] if p.title == "Requests over the TTFT gate"
    ]))
    error_message = "The TTFT table must hide traceIdHidden from the table with a custom.hideFrom.viz override."
  }
  assert {
    condition = alltrue([
      for p in jsondecode(grafana_dashboard.this["fleet-health.json"].config_json).panels :
      !strcontains(jsonencode(try(p.transformations, [])), "traceIdHidden")
      if p.title == "Requests over the TTFT gate"
    ])
    error_message = "No transformation on the TTFT table may name traceIdHidden: the Span ID link reads it from the data, so it is hidden with an override, not removed."
  }
}
