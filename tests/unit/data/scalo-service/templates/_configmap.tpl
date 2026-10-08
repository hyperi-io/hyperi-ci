{{- define "scalo-service.configmap" -}}
{{- $contract := .Files.Get "files/contract.json" | fromJson -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ $contract.app_name }}-config
data:
  image-digest: {{ .Values.image.digest | quote }}
  config.yaml: |
{{ toYaml .Values.config | indent 4 }}
{{- end -}}
