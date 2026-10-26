# NetMonitor dashboard. Standard library only, so no pip install step.
# python:slim always tracks the latest Python release; rebuild with --pull to update.
FROM python:slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NETMON_HOST=0.0.0.0 \
    NETMON_PORT=8080 \
    NETMON_CACHE=/data

RUN useradd --system --uid 10001 --home-dir /app netmon \
    && mkdir -p /data \
    && chown netmon:netmon /data

WORKDIR /app
COPY server.py ./
COPY static ./static

USER netmon
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/status' % os.environ.get('NETMON_PORT', '8080'), timeout=4)"

CMD ["python", "server.py"]
