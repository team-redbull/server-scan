FROM registry.access.redhat.com/ubi9/ubi-minimal:9.8 AS base

ARG PYTHON_VERSION=3.13.15
ARG PYTHON_BUILD_STANDALONE_RELEASE=20260807
ADD https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_BUILD_STANDALONE_RELEASE}/cpython-${PYTHON_VERSION}+${PYTHON_BUILD_STANDALONE_RELEASE}-x86_64-unknown-linux-gnu-install_only.tar.gz /tmp/python.tar.gz

RUN microdnf install -y --setopt=install_weak_deps=0 tar gzip shadow-utils \
    && tar -xzf /tmp/python.tar.gz -C /opt \
    && rm /tmp/python.tar.gz \
    && microdnf remove -y tar gzip \
    && microdnf clean all

ENV PATH="/opt/python/bin:${PATH}"

FROM base AS deps
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --no-compile -r requirements.txt

FROM base AS runtime

RUN groupadd -g 1001 app && useradd -u 1001 -g app -d /app -s /sbin/nologin app

COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY backend/app ./app
COPY tools ./tools
COPY .claude/skills/server-scan-api ./skill

# Required for OpenShift's arbitrary UID: useradd -d /app leaves it 0700. See deploy/README.md.
RUN chgrp -R 0 /app && chmod -R g=u /app

# PYTHONWARNINGS: `ucsmsdk` 0.9.18 writes its version regexes as non-raw
# strings, so every collector pod logs ~32 SyntaxWarnings the first time
# it compiles the package — which is every pod, since nothing here caches
# bytecode. Scoped to that one message, so any other SyntaxWarning still
# surfaces; `W605` gates our own at lint time. The pin is to what the
# air-gapped mirror carries, so upgrading is not available.
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONWARNINGS="ignore:invalid escape sequence:SyntaxWarning"

USER 1001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health/live', timeout=2).status == 200 else 1)"

ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
