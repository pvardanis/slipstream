# The dashboards, applied from the hand-authored JSON files in dashboards/ (ADR-0026,
# "Dashboards as code"): a dashboard is created, updated or deleted when its file is.

# Keyed by file name, so adding or removing a file touches only that dashboard. Panels
# name their data source as ${DS_PROMETHEUS}, ${DS_TEMPO} or ${DS_CLOUDWATCH}, which are
# replaced with the hosted Prometheus or Tempo data source's UID or the managed CloudWatch
# data source's, so the files carry no stack-specific UID.
resource "grafana_dashboard" "this" {
  for_each = fileset("${path.module}/dashboards", "*.json")

  config_json = replace(
    replace(
      replace(
        file("${path.module}/dashboards/${each.key}"),
        "$${DS_PROMETHEUS}",
        data.grafana_data_source.prometheus.uid,
      ),
      "$${DS_TEMPO}",
      data.grafana_data_source.tempo.uid,
    ),
    "$${DS_CLOUDWATCH}",
    grafana_data_source.cloudwatch.uid,
  )
}
