FROM python:3.12.11-slim-bookworm

ARG CLOUD_SQL_PROXY_VERSION=2.22.0
ARG CLOUD_SQL_PROXY_SHA256=42e73a8775bc3300b6514816cc4893f01b109a50f65aa779a451d3e4ff4c3ead

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl --fail --location --proto '=https' --tlsv1.2 \
      "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v${CLOUD_SQL_PROXY_VERSION}/cloud-sql-proxy.linux.amd64" \
      --output /usr/local/bin/cloud-sql-proxy \
    && echo "${CLOUD_SQL_PROXY_SHA256}  /usr/local/bin/cloud-sql-proxy" | sha256sum --check --strict \
    && chmod 0555 /usr/local/bin/cloud-sql-proxy \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt

COPY verify_restore.py .

USER 65532:65532
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
ENTRYPOINT ["python", "/app/verify_restore.py"]
