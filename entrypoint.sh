#!/bin/bash
set -e

# Railway/Render expose the public port via $PORT. Default to 8080 locally.
PORT="${PORT:-8080}"

echo "Starting gunicorn on port ${PORT}..."

# Single worker is intentional: each download spawns a Chromium instance
# (~150-300 MB) and holds response bodies in RAM. Multiple workers would
# multiply memory and easily blow Railway's free-tier 512 MB ceiling.
# Threads serve the SSE stream + healthchecks while a download runs.
exec gunicorn app:app \
    --bind "0.0.0.0:${PORT}" \
    --workers 1 \
    --threads 4 \
    --timeout 600 \
    --graceful-timeout 30 \
    --max-requests 50 \
    --max-requests-jitter 10 \
    --worker-class gthread \
    --access-logfile - \
    --error-logfile -
