{{- define "scalo-service.contract" -}}
{{- $raw := .Files.Get "files/contract.json" -}}
{{- if not $raw -}}
{{- fail "scalo-service: the chart has no files/contract.json" -}}
{{- end -}}
{{- $raw -}}
{{- end -}}
