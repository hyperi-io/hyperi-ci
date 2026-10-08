{{- define "scalo-service.configmap" -}}
{{- $contract := include "scalo-service.contract" . | fromJson -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ $contract.app_name }}-config
data:
  config.yaml: |
{{ toYaml .Values.config | indent 4 }}
{{- end -}}
