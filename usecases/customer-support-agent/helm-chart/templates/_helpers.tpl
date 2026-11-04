{{/*
Expand the name of the chart.
*/}}
{{- define "customer-support-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "customer-support-agent.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "customer-support-agent.labels" -}}
helm.sh/chart: {{ include "customer-support-agent.name" . }}-{{ .Chart.Version | replace "+" "_" }}
{{ include "customer-support-agent.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "customer-support-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "customer-support-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
