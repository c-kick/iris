FROM python:3.12-slim

# glibc: keep multi-MiB attachment buffers out of the heap so freed memory returns to the OS
ENV MALLOC_MMAP_THRESHOLD_=262144 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    NOTIFY_CONFIG=/config/notify.yaml \
    NOTIFY_DB=/data/notify.sqlite3 \
    NOTIFY_KEY_FILE=/secrets/master.key

RUN groupadd --system --gid 1026 notify \
    && useradd --system --uid 1026 --gid 1026 --home /app --shell /usr/sbin/nologin notify

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY notify/ ./notify/
RUN mkdir -p /data /config /secrets && chown notify:notify /data /secrets && chmod 700 /secrets

USER 1026:1026
EXPOSE 8930
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8930/health/live', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "notify", "serve"]
