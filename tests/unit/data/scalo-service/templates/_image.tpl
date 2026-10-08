{{- define "scalo-service.image" -}}
{{- $contract := include "scalo-service.contract" . | fromJson -}}
{{- $image := .Values.image | default dict -}}
{{- $repository := printf "%s/%s" (trimSuffix "/" $contract.image_registry) $contract.app_name -}}
{{- $reference := printf "%s:%s" $repository ($image.tag | default .Chart.AppVersion) -}}
{{- with $image.digest -}}{{- $reference = printf "%s@%s" $reference . -}}{{- end -}}
{{- $reference -}}
{{- end -}}
