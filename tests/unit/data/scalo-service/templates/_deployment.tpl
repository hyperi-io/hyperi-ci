{{- define "scalo-service.deployment" -}}
{{- $contract := include "scalo-service.contract" . | fromJson -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $contract.app_name }}
spec:
  replicas: {{ .Values.replicaCount | default 1 }}
  selector:
    matchLabels:
      app.kubernetes.io/name: {{ $contract.app_name }}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {{ $contract.app_name }}
    spec:
      containers:
        - name: {{ $contract.app_name }}
          image: {{ include "scalo-service.image" . }}
{{- end -}}
