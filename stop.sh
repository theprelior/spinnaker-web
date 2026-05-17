#!/bin/bash
# Stop all SpiNNaker2 services

if systemctl list-units --type=service | grep -q spinnaker; then
    sudo systemctl stop spinnaker-web spinnaker-celery
    echo "Services stopped."
else
    # Fallback: manual kill (before systemd setup)
    pkill -f "uvicorn main:app" 2>/dev/null && echo "FastAPI stopped." || true
    [ -f celery.pid ] && kill "$(cat celery.pid)" 2>/dev/null && echo "Celery stopped." || true
    rm -f celery.pid
fi
