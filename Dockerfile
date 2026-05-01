FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY runtime/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn==21.2.0

COPY runtime/orchestrator.py ./

# Fly maps to internal_port=8080 (see fly.toml). Override with $PORT.
ENV PORT=8080
EXPOSE 8080

# 1 worker × 4 threads is plenty for one user. The webhook handler ack-and-
# returns within milliseconds; the actual session work runs on the in-process
# ThreadPoolExecutor (see orchestrator.py).
CMD exec gunicorn \
    --workers 1 \
    --threads 4 \
    --bind 0.0.0.0:${PORT} \
    --access-logfile - \
    --error-logfile - \
    --timeout 30 \
    orchestrator:app
