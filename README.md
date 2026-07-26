# SpiNNaker2 Web Platform

A browser-based IDE for running Python simulations on **SpiNNaker2 neuromorphic hardware**.
Write or upload code in the browser, execute it on real hardware via Slurm, stream live
output, and view results — without SSH or a local SpiNNaker2 environment.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          User's Browser                                   │
│   Monaco Editor  │  Live Terminal (SSE)  │  Results Viewer (PNG/files)   │
└────────────────────────────┬─────────────────────────────────────────────┘
                             │  HTTPS  (Tailscale VPN)
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                    Fedora Laptop  (hostname: fedora)                      │
│                                                                           │
│  ┌─────────────────────┐     ┌────────────────────────────────────────┐  │
│  │  FastAPI  :8000     │     │  Redis                                 │  │
│  │  • Auth (JWT)       │◄───►│  • Celery broker                       │  │
│  │  • Job submission   │     │  • Live log pub/sub                    │  │
│  │  • SSE log stream   │     │  • Job metadata & status               │  │
│  │  • Hardware status  │     │  • Rate limit counters                 │  │
│  │  • REST API         │     └────────────────────────────────────────┘  │
│  └──────────┬──────────┘                                                 │
│             │                                                             │
│             ▼                                                             │
│  ┌─────────────────────┐                                                 │
│  │  Celery Worker      │  tail -F Slurm output → Redis pub/sub           │
│  │  concurrency=1      │                                                 │
│  └──────────┬──────────┘                                                 │
│             │ sbatch --gres=spinnaker:1 --qos=webqos                     │
│             ▼                                                             │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  Slurm  (slurmctld + slurmd + slurmdbd)                         │    │
│  │                                                                  │    │
│  │  GRES spinnaker:1  ← hardware mutex (count=1)                   │    │
│  │  adminqos (priority 100)  >  webqos (priority 10, max 30 min)   │    │
│  │  Preemption: admin requeues web jobs                             │    │
│  └──────────────────────────────┬───────────────────────────────────┘    │
│                                 │  wrapper.sh · spinnaker2new conda env  │
│                                 ▼                                         │
│                    ┌────────────────────────┐                            │
│                    │   SpiNNcloud Device    │                            │
│                    │   48 SpiNNaker2 chips  │                            │
│                    │   IP: 192.168.1.17     │                            │
│                    └────────────────────────┘                            │
└──────────────────────────────────────────────────────────────────────────┘

SSH Admin (Tailscale) ──► srun --gres=spinnaker:1 --qos=adminqos ──► Slurm ──► hardware
```

### Job Submission Flow

```
Browser              FastAPI            Redis          Celery Worker       Slurm
   │                    │                 │                 │                │
   │── POST /jobs ──────►│                 │                 │                │
   │                    │── apply_async ──────────────────►│                │
   │◄─ {job_id} ────────│                 │                 │                │
   │                    │                 │                 │── sbatch ──────►│
   │── GET /jobs/id/logs►│                 │                 │                │
   │   (SSE stream)     │── subscribe ───►│                 │                │
   │                    │                 │  tail -F .out   │                │
   │                    │                 │◄── publish ─────│                │
   │◄─ data: log line ──│◄── message ─────│                 │                │
   │◄─ data: __DONE__ ──│◄── message ─────│                 │                │
```

---

## Features

- **Monaco Editor** — VSCode's editor engine with Python syntax highlighting
- **Live terminal output** — Server-Sent Events stream Slurm job logs in real time
- **In-browser results viewer** — PNG plots displayed without downloading
- **Hardware serialization** — Slurm GRES `spinnaker:1` ensures one job at a time
- **Hardware status** — Online / Busy / Offline indicator from `sinfo` node state
- **Admin preemption** — SSH admin jobs interrupt web jobs (web jobs requeue automatically)
- **Template library** — Example SpiNNaker2 scripts in the sidebar
- **Save & load code** — Per-user snippets stored in SQLite
- **User authentication** — JWT + bcrypt, registration can be disabled after setup
- **Rate limiting** — Login brute-force protection, per-user hourly job limits
- **Stop running jobs** — Calls `scancel` on the Slurm job ID from the browser
- **Tailscale access** — No open ports; server reachable only via VPN overlay

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Vanilla HTML/JS + Monaco Editor |
| API | FastAPI + Uvicorn |
| Task queue | Celery (concurrency=1) |
| Broker / cache | Redis |
| Database | SQLite (SQLAlchemy async) |
| Job scheduler | Slurm (sbatch / scancel / sinfo / squeue) |
| Execution env | Conda `spinnaker2new` via `/opt/slurm-wrappers/wrapper.sh` |
| Auth | JWT (`python-jose`) + bcrypt (`passlib`) |
| Process manager | systemd |
| Network | Tailscale VPN |

---

## Project Structure

```
spinnaker-web/
├── main.py              # FastAPI app — all API endpoints
├── auth.py              # JWT authentication
├── models.py            # SQLAlchemy models (User, SavedCode)
├── database.py          # Async SQLite connection
├── celery_app.py        # Celery configuration
├── tasks.py             # Celery task: sbatch submission + log tailing
├── sandbox.py           # Environment config (conda env, safe env vars)
│
├── static/
│   └── index.html       # Single-page frontend
│
├── templates/           # Example SpiNNaker2 scripts (sidebar)
│
├── setup.sh             # One-command Fedora installer
├── .env.example         # Configuration template
└── .gitignore
```

---

## Installation (Fedora)

```bash
git clone https://github.com/theprelior/spinnaker-web.git
cd spinnaker-web
bash setup.sh
```

`setup.sh` automatically:
1. Installs Redis
2. Creates Python virtualenv and installs dependencies
3. Generates a random JWT secret
4. Detects the `spinnaker2new` conda environment and sets `SPINNAKER_PYTHON`
5. Copies `.env` to `/etc/spinnaker-web.env` (SELinux-safe, mode 600)
6. Registers and starts `spinnaker-web` and `spinnaker-celery` systemd services

> **Requires**: Slurm already installed and running (see [spinncloud-slurm](https://github.com/theprelior/spinncloud-slurm))

---

## Configuration

Edit `/etc/spinnaker-web.env` on the Fedora machine:

```env
JWT_SECRET=<64-char random hex>
ALLOW_REGISTRATION=true           # set false after creating accounts
SPINNAKER_PYTHON=/home/geb/.conda/envs/spinnaker2new/bin/python3
SPINNAKER_DIR=/mnt/spinnaker      # SpinnmanSession lock files location
S2_IP48=192.168.1.17              # SpiNNaker2 board IP
MAX_JOBS_PER_HOUR=10
JOB_TIMEOUT_SECONDS=300
BOARD_MANAGEMENT_IP=192.168.1.2   # STM management board (keepalive)
```

After editing:
```bash
sudo systemctl restart spinnaker-web spinnaker-celery
```

---

## Service Management

```bash
# Status
sudo systemctl status spinnaker-web spinnaker-celery

# Restart
sudo systemctl restart spinnaker-web spinnaker-celery

# Logs
sudo journalctl -u spinnaker-web   -f
sudo journalctl -u spinnaker-celery -f
```

---

## Development Workflow

```bash
# 1. Make changes locally, push to GitHub
git add . && git commit -m "..." && git push

# 2. Pull on Fedora
cd ~/Desktop/spinnaker-web
git pull origin main
sudo systemctl restart spinnaker-web spinnaker-celery
```

---

## Security

| Layer | Mechanism |
|-------|-----------|
| Network | Tailscale VPN — no public ports |
| Auth | JWT (24h expiry) + bcrypt |
| Brute force | 5 login attempts / 15 min per IP |
| Job rate | Per-user hourly limit (`MAX_JOBS_PER_HOUR`) |
| Hardware | Slurm GRES serialization — one job at a time |
| Execution | Conda env isolation, thread limits, wall-clock timeout |
| Secrets | `/etc/spinnaker-web.env` mode 600 |
| Headers | `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy` |

---

## User Documentation

See [docs/USER_GUIDE.md](docs/USER_GUIDE.md) for a step-by-step guide with screenshots.
