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

{{- /* The API and every collector CronJob. Collectors need the cursor secret too, or Settings refuses to start. */ -}}
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
`GET /servers/available`'s per-manager recheck timeouts, `TYPE:seconds,...`: each collector's own
configured per-call timeout, so the API waits as long as that collector's CronJob does (ADR-0032).
A key a values file lacks is skipped, which leaves the shared 15 s default for that manager.
*/ -}}
{{- define "serverScan.recheckTimeouts" -}}
{{- $c := .Values.collectors | default dict -}}
{{- $parts := list -}}
{{- with $c.ucsCentral }}{{- if hasKey . "timeoutSeconds" }}{{- $parts = append $parts (printf "UCS_CENTRAL:%v" .timeoutSeconds) }}{{- end }}{{- end -}}
{{- with $c.intersight }}{{- if hasKey . "connectTimeoutSeconds" }}{{- $parts = append $parts (printf "INTERSIGHT:%v" .connectTimeoutSeconds) }}{{- end }}{{- end -}}
{{- with $c.oneview }}{{- if hasKey . "timeoutSeconds" }}{{- $parts = append $parts (printf "ONEVIEW:%v" .timeoutSeconds) }}{{- end }}{{- end -}}
{{- with $c.ome }}{{- if hasKey . "timeoutSeconds" }}{{- $parts = append $parts (printf "OPENMANAGE:%v" .timeoutSeconds) }}{{- end }}{{- end -}}
{{- join "," $parts -}}
{{- end -}}
