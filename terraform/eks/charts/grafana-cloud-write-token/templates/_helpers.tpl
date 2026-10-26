{{/*
Named templates for the grafana-cloud-write-token chart.
The SecretStore's name is defined once, so the ExternalSecret's secretStoreRef always names
the store this chart creates.
*/}}
{{- define "grafana-cloud-write-token.storeName" -}}
aws-secrets-manager
{{- end -}}
