#!/bin/bash
# Bu scripti LOCAL makinede çalıştır — projeyi Fedora'ya kopyalar ve kurar.
#
# Kullanım: bash deploy.sh [kullanici@ip]
# Örnek:    bash deploy.sh theprelior@192.168.1.195
#           bash deploy.sh theprelior@100.104.85.76

set -e

TARGET="${1:-geb@100.104.85.76}"
REMOTE_DIR="~/Desktop/spinnaker-web"

echo "Deploying to $TARGET:$REMOTE_DIR ..."

rsync -avz --exclude='.venv' \
           --exclude='__pycache__' \
           --exclude='*.pyc' \
           --exclude='uploads/*' \
           --exclude='results/*' \
           --exclude='job_outputs/*' \
           --exclude='spinnaker.db' \
           --exclude='celery.log' \
           --exclude='celery.pid' \
           --exclude='.env' \
           "$(dirname "$0")/" "$TARGET:$REMOTE_DIR/"

echo "Files copied. Running setup on remote..."
ssh -t "$TARGET" "cd $REMOTE_DIR && bash setup.sh"
