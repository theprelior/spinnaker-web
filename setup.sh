#!/bin/bash
# SpiNNaker2 Web Interface — Fedora Setup Script
# Kullanım: bash setup.sh
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[x]${NC} $1"; exit 1; }
step()  { echo -e "\n${BLUE}━━━ $1 ━━━${NC}"; }

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"
VENV="$PROJECT_DIR/.venv"

echo -e "${BLUE}"
echo "  ███████╗██████╗ ██╗███╗   ██╗███╗   ██╗ █████╗ ██╗  ██╗███████╗██████╗ ██████╗ "
echo "  ██╔════╝██╔══██╗██║████╗  ██║████╗  ██║██╔══██╗██║ ██╔╝██╔════╝██╔══██╗╚════██╗"
echo "  ███████╗██████╔╝██║██╔██╗ ██║██╔██╗ ██║███████║█████╔╝ █████╗  ██████╔╝ █████╔╝"
echo "  ╚════██║██╔═══╝ ██║██║╚██╗██║██║╚██╗██║██╔══██║██╔═██╗ ██╔══╝  ██╔══██╗██╔═══╝ "
echo "  ███████║██║     ██║██║ ╚████║██║ ╚████║██║  ██║██║  ██╗███████╗██║  ██║███████╗"
echo "  ╚══════╝╚═╝     ╚═╝╚═╝  ╚═══╝╚═╝  ╚═══╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚══════╝"
echo -e "${NC}"
echo "  Web Interface — Fedora Setup"
echo "  Project : $PROJECT_DIR"
echo "  User    : $USER_NAME"

# ── 1. Redis ──────────────────────────────────────────────────────────────────
step "Redis"
if ! command -v redis-cli &>/dev/null; then
    info "Installing Redis via dnf..."
    sudo dnf install -y redis
else
    info "Redis already installed."
fi
sudo systemctl enable --now redis
sleep 1
redis-cli ping | grep -q PONG && info "Redis is running." || error "Redis failed to start."

# ── 2. Python venv ────────────────────────────────────────────────────────────
step "Python Environment"
if [ ! -d "$VENV" ]; then
    info "Creating virtual environment..."
    python3 -m venv "$VENV"
fi
info "Installing dependencies..."
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.32.0" \
    "celery>=5.4.0" \
    "redis>=5.2.0" \
    "python-multipart>=0.0.18" \
    "python-jose[cryptography]>=3.3.0" \
    "passlib[bcrypt]>=1.7.4" \
    "bcrypt==4.0.1" \
    "sqlalchemy>=2.0.0" \
    "aiosqlite>=0.20.0" \
    "python-dotenv>=1.0.0" \
    "pydantic[email]"
info "Python environment ready."

# ── 3. .env ───────────────────────────────────────────────────────────────────
step "Configuration"
if [ ! -f "$PROJECT_DIR/.env" ]; then
    warn ".env not found — creating from template..."
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s/CHANGE_THIS_TO_A_RANDOM_SECRET/$SECRET/" "$PROJECT_DIR/.env"
    info "JWT secret auto-generated."
else
    info ".env already exists — skipping."
fi

# Detect conda spinnaker2 env
CONDA_PYTHON=""
for p in \
    "$HOME/miniconda3/envs/spinnaker2/bin/python3" \
    "$HOME/anaconda3/envs/spinnaker2/bin/python3" \
    "/opt/conda/envs/spinnaker2/bin/python3"; do
    [ -f "$p" ] && CONDA_PYTHON="$p" && break
done

if [ -n "$CONDA_PYTHON" ]; then
    if grep -q "^SPINNAKER_PYTHON=" "$PROJECT_DIR/.env"; then
        sed -i "s|^SPINNAKER_PYTHON=.*|SPINNAKER_PYTHON=$CONDA_PYTHON|" "$PROJECT_DIR/.env"
    else
        echo "SPINNAKER_PYTHON=$CONDA_PYTHON" >> "$PROJECT_DIR/.env"
    fi
    info "SpiNNaker2 conda Python: $CONDA_PYTHON"
else
    warn "conda env 'spinnaker2' not found — scripts will use system Python."
fi

# .env → /etc/spinnaker-web.env (SELinux uyumlu konum)
sudo cp "$PROJECT_DIR/.env" /etc/spinnaker-web.env
sudo chmod 600 /etc/spinnaker-web.env
ENV_FILE=/etc/spinnaker-web.env

# ── 4. Systemd services ───────────────────────────────────────────────────────
step "Systemd Services"

sudo tee /etc/systemd/system/spinnaker-web.service > /dev/null << EOF
[Unit]
Description=SpiNNaker2 Web Interface (FastAPI)
Documentation=https://github.com/your-repo
After=network.target redis.service
Requires=redis.service

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=spinnaker-web

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/spinnaker-celery.service > /dev/null << EOF
[Unit]
Description=SpiNNaker2 Celery Worker
After=network.target redis.service
Requires=redis.service

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/celery -A celery_app worker --loglevel=info --concurrency=1
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=spinnaker-celery

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable spinnaker-web spinnaker-celery
info "Services registered and enabled."

# SELinux: allow systemd to execute venv binaries from home directory
if command -v chcon &>/dev/null && [ "$(getenforce 2>/dev/null)" != "Disabled" ]; then
    sudo chcon -R -t bin_t "$VENV/bin/" 2>/dev/null && info "SELinux context set for .venv/bin." || true
fi

# ── 5. Start ──────────────────────────────────────────────────────────────────
step "Starting Services"
sudo systemctl restart spinnaker-web spinnaker-celery
sleep 4

WEB_STATUS=$(sudo systemctl is-active spinnaker-web)
CEL_STATUS=$(sudo systemctl is-active spinnaker-celery)

[ "$WEB_STATUS"  = "active" ] && info "spinnaker-web    : running ✓" || warn "spinnaker-web    : $WEB_STATUS"
[ "$CEL_STATUS" = "active" ] && info "spinnaker-celery : running ✓" || warn "spinnaker-celery : $CEL_STATUS"

# ── 6. Summary ────────────────────────────────────────────────────────────────
TAILSCALE_IP=$(ip addr show tailscale0 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d/ -f1)
LOCAL_IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}━━━ Setup Complete ━━━${NC}"
echo ""
echo "  Access:"
echo "    Local     →  http://localhost:8000"
[ -n "$LOCAL_IP"     ] && echo "    Network   →  http://$LOCAL_IP:8000"
[ -n "$TAILSCALE_IP" ] && echo "    Tailscale →  http://$TAILSCALE_IP:8000"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status  spinnaker-web spinnaker-celery"
echo "    sudo systemctl restart spinnaker-web spinnaker-celery"
echo "    sudo journalctl -u spinnaker-web   -f   # FastAPI logs"
echo "    sudo journalctl -u spinnaker-celery -f  # Celery logs"
echo ""
echo "  First login: Register at the web interface."
echo "  Disable registration after setup: set ALLOW_REGISTRATION=false in .env"
echo "  then run: sudo systemctl restart spinnaker-web"
echo ""
