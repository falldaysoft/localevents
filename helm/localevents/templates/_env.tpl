{{/*
Shared environment for the web, worker, and cron pods. They run the same image
and need the same configuration; only the command differs.
*/}}
{{- define "localevents.env" -}}
- name: DEBUG
  value: "false"
- name: ALLOWED_HOSTS
  value: "{{ .Values.host }},www.{{ .Values.host }}"
- name: CSRF_TRUSTED_ORIGINS
  value: "https://{{ .Values.host }},https://www.{{ .Values.host }}"
- name: SITE_NAME
  value: {{ .Values.site.name | quote }}
- name: SITE_TAGLINE
  value: {{ .Values.site.tagline | quote }}
- name: CONTACT_EMAIL
  value: {{ .Values.site.contactEmail | quote }}
- name: SITE_TIMEZONE
  value: {{ .Values.site.timezone | quote }}
- name: MAP_CENTER_LAT
  value: {{ .Values.site.mapCenterLat | quote }}
- name: MAP_CENTER_LNG
  value: {{ .Values.site.mapCenterLng | quote }}
- name: MAP_ZOOM
  value: {{ .Values.site.mapZoom | quote }}
- name: MAP_BBOX
  value: {{ .Values.site.mapBbox | quote }}
- name: TILE_URL
  value: {{ .Values.site.tileUrl | quote }}
- name: TILE_ATTRIBUTION
  value: {{ .Values.site.tileAttribution | quote }}
- name: EMAIL_BACKEND
  value: "django.core.mail.backends.smtp.EmailBackend"
- name: EMAIL_HOST
  value: {{ .Values.email.host | quote }}
- name: EMAIL_PORT
  value: {{ .Values.email.port | quote }}
- name: EMAIL_USE_TLS
  value: {{ .Values.email.useTls | quote }}
- name: DEFAULT_FROM_EMAIL
  value: {{ .Values.email.defaultFrom | quote }}
- name: SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secretsName }}
      key: secret-key
- name: EMAIL_HOST_USER
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secretsName }}
      key: email-host-user
      optional: true
- name: EMAIL_HOST_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secretsName }}
      key: email-host-password
      optional: true
- name: ANTHROPIC_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secretsName }}
      key: anthropic-api-key
      optional: true
- name: OPENROUTER_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secretsName }}
      key: openrouter-api-key
      optional: true
{{- end -}}
