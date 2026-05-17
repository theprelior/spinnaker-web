#!/bin/bash
# Start all SpiNNaker2 services

if systemctl list-units --type=service | grep -q spinnaker; then
    sudo systemctl restart spinnaker-web spinnaker-celery
    sleep 2
    bash "$(dirname "$0")/status.sh"
else
    echo "Systemd services not installed. Run setup.sh first."
    echo "Falling back to manual start..."
    cd "$(dirname "$0")"

    redis-cli ping &>/dev/null || { echo "ERROR: Redis not running."; exit 1; }

    .venv/bin/celery -A celery_app worker \
        --loglevel=info --concurrency=1 \
        --logfile=celery.log --pidfile=celery.pid --detach

    .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
fi
