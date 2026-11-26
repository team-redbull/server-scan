{{- /* Blank tag means the chart's appVersion. Never `latest`. Every top-level block is read through `default dict`: an older values file may lack it. */ -}}
{{- define "collectors.image" -}}
{{- $img := .Values.image | default dict -}}
{{ $img.repository | default "ghcr.io/team-redbull/server-scan-api" }}:{{ $img.tag | default .Chart.AppVersion }}
{{- end -}}

{{- /* Object-name prefix. `fullnameOverride` lets a hub release named differently (`server-scan-collectors`) keep the platform's own `server-scan-collector-*` names and so adopt its CronJobs in place (ADR-0060). */ -}}
{{- define "collectors.fullname" -}}
{{ .Values.fullnameOverride | default .Release.Name }}
{{- end -}}

{{- define "collectors.pullPolicy" -}}
{{ (.Values.image | default dict).pullPolicy | default "IfNotPresent" }}
{{- end -}}

{{- /* "true" or nothing: the chart consumes the API release's ConfigMap AND auth Secret (the hub shape), so INVENTORY_AUTH_ENABLED and INVENTORY_MONGO_DB come from that ConfigMap and are not pinned here (ADR-0060). Needs both: a ConfigMap saying auth is on, with no auth Secret mounted, would crash-loop the collector. */ -}}
{{- define "collectors.consumesApiConfig" -}}
{{- if and (.Values.config | default dict).existingConfigMap (.Values.auth | default dict).existingSecret }}true{{ end -}}
{{- end -}}

{{- define "collectors.authSecretName" -}}
{{ (.Values.auth | default dict).existingSecret }}
{{- end -}}

{{- /* A collector run never opens Redis, so INVENTORY_REDIS_URI is rendered only when asked: a literal `db.redisUri`, else a Secret named `db.redisUriSecretName`. Settings refuses to start in production without the cursor secret. */ -}}
{{- define "collectors.dbEnv" -}}
{{- $db := .Values.db | default dict -}}
- name: INVENTORY_MONGO_URI
  valueFrom:
    secretKeyRef:
      name: {{ $db.secretName | default "server-scan-db" }}
      key: {{ $db.mongoUriKey | default "mongo-uri" }}
{{- if $db.redisUri }}
- name: INVENTORY_REDIS_URI
  value: {{ $db.redisUri | quote }}
{{- else if $db.redisUriSecretName }}
- name: INVENTORY_REDIS_URI
  valueFrom:
    secretKeyRef:
      name: {{ $db.redisUriSecretName }}
      key: {{ $db.redisUriKey | default "redis-uri" }}
{{- end }}
- name: INVENTORY_CURSOR_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ $db.cursorSecretName | default "server-scan-cursor-secret" }}
      key: {{ $db.cursorSecretKey | default "cursor-secret" }}
{{- if not (include "collectors.consumesApiConfig" .) }}
- name: INVENTORY_MONGO_DB
  value: {{ $db.dbName | default "server-scan" | quote }}
{{- end }}
{{- include "collectors.warrantyEnv" . | nindent 0 }}
{{- end -}}

{{- /* Explicit env beats envFrom: a consumed server-scan ConfigMap may say INVENTORY_AUTH_ENABLED=true, which would demand the API's AD secrets, so it is pinned false unless the auth Secret is mounted too. */ -}}
{{- define "collectors.commonEnv" -}}
- name: INVENTORY_ENVIRONMENT
  value: production
{{- if not (include "collectors.consumesApiConfig" .) }}
- name: INVENTORY_AUTH_ENABLED
  value: "false"
{{- end }}
{{- end -}}

{{- define "collectors.configMapName" -}}
{{ (.Values.config | default dict).existingConfigMap | default (printf "%s-config" (include "collectors.fullname" .)) }}
{{- end -}}

{{- define "collectors.credentialsSecretName" -}}
{{ (.Values.collectors | default dict).existingSecret | default (printf "%s-collector-credentials" (include "collectors.fullname" .)) }}
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

{{- /* The warranty lookup for a server a collector creates (ADR-0070). The URI is set once, in the server-scan release's `warranty`; this chart only reads that release's Secret, optionally, so warranty off (no Secret) is no error. */ -}}
{{- define "collectors.warrantyEnv" -}}
{{- $w := .Values.warranty | default dict -}}
- name: INVENTORY_WARRANTY_MONGO_URI
  valueFrom:
    secretKeyRef:
      name: {{ $w.secretName | default "server-scan-warranty-db" }}
      key: {{ $w.secretKey | default "mongo-uri" }}
      optional: true
- name: INVENTORY_WARRANTY_DATABASE
  value: {{ $w.database | default "MongoDB-Nitur" | quote }}
- name: INVENTORY_WARRANTY_COLLECTION
  value: {{ $w.collection | default "warranty-excel-new" | quote }}
{{- end -}}
