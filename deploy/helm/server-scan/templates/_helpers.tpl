{{- /* Emit "true" or nothing: a bare `and` returns the string "false", which every Helm `if` reads as truthy. */ -}}
{{- define "serverScan.composeMongoUri" -}}
{{- if and .Values.mongodb.enabled (not .Values.mongodb.auth.existingSecret) -}}true{{- end -}}
{{- end -}}

{{- define "serverScan.composeRedisUri" -}}
{{- if and .Values.redis.enabled (not .Values.redis.auth.existingSecret) -}}true{{- end -}}
{{- end -}}

{{- define "serverScan.mongoSecret" -}}
{{- if include "serverScan.composeMongoUri" . -}}
{{ .Release.Name }}-bundled-db
{{- else -}}
{{ .Values.db.secretName }}
{{- end -}}
{{- end -}}

{{- define "serverScan.redisSecret" -}}
{{- if include "serverScan.composeRedisUri" . -}}
{{ .Release.Name }}-bundled-db
{{- else -}}
{{ .Values.db.secretName }}
{{- end -}}
{{- end -}}

{{- /* The API and both prune CronJobs. The prunes need the cursor secret too, or Settings refuses to start. */ -}}
{{- define "serverScan.dbEnv" -}}
- name: INVENTORY_MONGO_URI
  valueFrom:
    secretKeyRef:
      name: {{ include "serverScan.mongoSecret" . }}
      key: mongo-uri
- name: INVENTORY_REDIS_URI
  valueFrom:
    secretKeyRef:
      name: {{ include "serverScan.redisSecret" . }}
      key: redis-uri
{{- if or .Values.backend.cursorSecret .Values.backend.existingCursorSecret }}
- name: INVENTORY_CURSOR_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.backend.existingCursorSecret | default (printf "%s-cursor-secret" .Release.Name) }}
      key: cursor-secret
{{- end }}
{{- end -}}

{{- /* Blank tag means the chart's appVersion. Never `latest` — see deploy/README.md. */ -}}
{{- define "serverScan.apiImage" -}}
{{ .Values.backend.image.repository }}:{{ .Values.backend.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "serverScan.frontendImage" -}}
{{ .Values.frontend.image.repository }}:{{ .Values.frontend.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- /*
`GET /servers/available`'s per-manager recheck timeouts, `TYPE:seconds,...`, from `recheck.timeouts`
(ADR-0032). A type the map lacks keeps the shared 15 s default. A values file with no `recheck`
block at all (the platform repo's, until it adds one) gets values.yaml's defaults, not nothing.
*/ -}}
{{- define "serverScan.recheckTimeouts" -}}
{{- $t := dict "UCS_CENTRAL" 240 "INTERSIGHT" 10 -}}
{{- if kindIs "map" .Values.recheck }}{{- $t = .Values.recheck.timeouts | default dict }}{{- end -}}
{{- $parts := list -}}
{{- range $type, $seconds := $t }}{{- $parts = append $parts (printf "%s:%v" $type $seconds) }}{{- end -}}
{{- join "," $parts -}}
{{- end -}}
