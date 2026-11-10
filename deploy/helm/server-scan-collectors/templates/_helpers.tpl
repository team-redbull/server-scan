{{- /* Blank tag means the chart's appVersion. Never `latest`. Every top-level block is read through `default dict`: an older values file may lack it. */ -}}
{{- define "collectors.image" -}}
{{- $img := .Values.image | default dict -}}
{{ $img.repository | default "ghcr.io/team-redbull/server-scan-api" }}:{{ $img.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "collectors.pullPolicy" -}}
{{ (.Values.image | default dict).pullPolicy | default "IfNotPresent" }}
{{- end -}}

{{- /* Collectors write to MongoDB only (never Redis); Settings refuses to start in production without the cursor secret. */ -}}
{{- define "collectors.dbEnv" -}}
{{- $db := .Values.db | default dict -}}
- name: INVENTORY_MONGO_URI
  valueFrom:
    secretKeyRef:
      name: {{ $db.secretName | default "server-scan-db" }}
      key: {{ $db.mongoUriKey | default "mongo-uri" }}
- name: INVENTORY_CURSOR_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ $db.cursorSecretName | default "server-scan-cursor-secret" }}
      key: {{ $db.cursorSecretKey | default "cursor-secret" }}
- name: INVENTORY_MONGO_DB
  value: {{ $db.dbName | default "server-scan" | quote }}
{{- end -}}

{{- /* Explicit env beats envFrom: a consumed server-scan ConfigMap may say INVENTORY_AUTH_ENABLED=true, which would demand the API's AD secrets. */ -}}
{{- define "collectors.commonEnv" -}}
- name: INVENTORY_ENVIRONMENT
  value: production
- name: INVENTORY_AUTH_ENABLED
  value: "false"
{{- end -}}

{{- define "collectors.configMapName" -}}
{{ (.Values.config | default dict).existingConfigMap | default (printf "%s-config" .Release.Name) }}
{{- end -}}

{{- define "collectors.credentialsSecretName" -}}
{{ (.Values.collectors | default dict).existingSecret | default (printf "%s-collector-credentials" .Release.Name) }}
{{- end -}}

{{- /* `ternary`, not `default true`: `default` reads an explicit false as unset. */ -}}
{{- define "collectors.labels" -}}
{{- if ternary .Values.triggerable true (hasKey .Values "triggerable") }}
server-scan.io/triggerable: "true"
{{- end }}
app.kubernetes.io/component: collector
app.kubernetes.io/part-of: server-scan
{{- end -}}

{{- /* "true" or nothing: whether any collector is on, so an empty values file renders no ConfigMap or Secret. */ -}}
{{- define "collectors.anyEnabled" -}}
{{- $c := .Values.collectors | default dict -}}
{{- range $name := list "ucsCentral" "intersight" "oneview" "ome" "redfishStandalone" "kubevirt" "fake" -}}
{{- if (get $c $name | default dict).enabled }}true{{ end -}}
{{- end -}}
{{- end -}}

{{- define "collectors.securityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
readOnlyRootFilesystem: true
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end -}}
