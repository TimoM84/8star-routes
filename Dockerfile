# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATA_DIR=/data PORT=8080 ENV_FILE=/nonexistent

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts/backup.py scripts/restore.py scripts/check_upgrade.py ./scripts/

RUN useradd --system --uid 10001 --home /app routes && mkdir -p /data && chown -R routes:routes /data
USER routes
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+os.environ.get('BASE_PATH','')+'/healthz', timeout=4).status==200 else 1)"

# Uvicorn handles SIGTERM gracefully; the scheduler finishes or interrupts its cycle before exit.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "app"]
