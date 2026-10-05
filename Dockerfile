FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ONYX_CLOUD_MODE=true \
    ONYX_BIND_HOST=0.0.0.0 \
    PORT=8765

RUN apt-get update \
    && apt-get install -y --no-install-recommends lsof procps ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt
COPY . ./
RUN mkdir -p /app/work /app/reports

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=3).read()"

CMD ["python", "-u", "dashboard.py"]
