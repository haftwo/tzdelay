FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY tzdelay ./tzdelay

# No config file is needed: stations are managed in the web page and saved
# under /data. Mount /config/config.json only to change server settings.
ENV TZDELAY_CONFIG=/config/config.json \
    TZDELAY_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/status',timeout=4)"

LABEL org.opencontainers.image.title="tzdelay" \
      org.opencontainers.image.description="Timezone delay for radio HLS streams"

CMD ["python", "-m", "tzdelay"]
