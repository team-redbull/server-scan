{{- define "nodesStatus.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- /* Both jobs write to MongoDB directly; Settings refuses to start without the cursor secret. */ -}}
{{- define "nodesStatus.dbEnv" -}}
- name: INVENTORY_MONGO_URI
  valueFrom:
    secretKeyRef:
      name: {{ .Values.db.secretName }}
      key: {{ .Values.db.mongoUriKey }}
- name: INVENTORY_CURSOR_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.db.cursorSecretName }}
      key: {{ .Values.db.cursorSecretKey }}
- name: INVENTORY_MONGO_DB
  value: {{ .Values.db.dbName | default "server-scan" | quote }}
{{- end -}}

{{- define "nodesStatus.securityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
readOnlyRootFilesystem: true
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end -}}
