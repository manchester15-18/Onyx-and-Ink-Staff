FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ONYX_CLOUD_MODE=true \
    ONYX_BIND_HOST=0.0.0.0 \
    PORT=8765

RUN apt-get update \
    && apt-get install -y --no-install-recommends git lsof procps ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Node 20 LTS (Debian trixie package, 20.19.x) for npm-based Jordan tools:
# npx skill installs, and the Impeccable detector when run via npm. The
# detector itself also ships as a self-contained binary in the storefront,
# so Node is an enabler, not a requirement of the detector binary.
# Impeccable 4.7 is installed globally so Jordan's detector tool works in
# this container without touching the storefront's node_modules.
RUN npm install -g impeccable@4.7.0

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt
RUN python -m playwright install --with-deps chromium
COPY . ./
RUN mkdir -p /app/work /app/reports

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=3).read()"

CMD ["python", "-u", "dashboard.py"]
