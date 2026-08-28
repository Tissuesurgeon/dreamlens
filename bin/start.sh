#!/bin/sh
# Railway (and similar hosts) inject PORT. Binding only :8000 makes the
# public URL time out with "Application failed to respond".
set -eu
PORT="${PORT:-8000}"
WORKERS="${WEB_CONCURRENCY:-1}"
exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WORKERS}" \
  --timeout 120 \
  --graceful-timeout 30 \
  --access-logfile - \
  --error-logfile -
