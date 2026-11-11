{{/*
Named templates for the slipstream-apps chart.
*/}}

{{/*
The sync policy every child Application shares: automated, with prune and self-heal, so
the tracked revision is what runs, and server-side apply, so a field dropped from git
(an autoscaled workload's replicas) is released rather than reset. Sync options are per
Application, so each child includes this; test/chart-render-test.sh fails a rendered
Application without ServerSideApply=true.
*/}}
{{- define "slipstream-apps.syncPolicy" -}}
automated:
  prune: true
  selfHeal: true
syncOptions:
  - ServerSideApply=true
{{- end }}
