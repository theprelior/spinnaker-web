# SpiNNaker2 Playground

A web-based IDE for running Python experiments on **SpiNNaker2 neuromorphic hardware**. Upload or write code in the browser, execute it on real hardware, stream live output, and visualize results — all without SSH or a local SpiNNaker2 environment.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          User's Browser                                  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Monaco Editor  │  Live Terminal (SSE)  │  Results Viewer (PNG)   │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────┬────────────────────────────────────────────┘
                             │  HTTP / SSE  (Tailscale VPN)
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     Fedora Machine (IdeaPad)                             │
│                                                                          │
│  ┌──────────────────────┐      ┌─────────────────────────────────────┐  │
│  │   FastAPI (port 8000) │      │           Redis (Valkey)            │  │
│  │                      │◄────►│                                     │  │
│  │  • Auth (JWT)        │      │  • Job queue (Celery broker)        │  │
│  │  • Job submission    │      │  • Live log pub/sub                 │  │
│  │  • SSE log stream    │      │  • Job metadata & history           │  │
│  │  • File serving      │      │  • Rate limit counters              │  │
│  │  • REST API          │      └─────────────────────────────────────┘  │
│  └──────────┬───────────┘                      ▲                        │
│             │  Submit task                      │ Publish logs           │
│             ▼                                   │                        │
│  ┌──────────────────────┐      ┌────────────────┴────────────────────┐  │
│  │   Celery Worker      │      │         Sandbox (sandbox.py)        │  │
│  │  (concurrency=1)     │─────►│                                     │  │
│  │                      │      │  • spinnaker2 conda environment     │  │
│  │  • Job queue         │      │  • Resource limits (CPU/RAM/files)  │  │
│  │  • Stop on request   │      │  • Wall-clock timeout               │  │
│  └──────────────────────┘      │  • Stripped environment vars        │  │
│                                └────────────────────────────────────-┘  │
│                                               │                          │
│                                               ▼                          │
│                                ┌─────────────────────────────────────┐  │
│                                │   SpiNNaker2 Hardware               │  │
│                                │   /home/geb/.conda/envs/spinnaker2  │  │
│                                └─────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

### Request Flow

```
Browser                FastAPI              Redis           Celery Worker
   │                      │                   │                  │
   │── POST /jobs ────────►│                   │                  │
   │                      │── hset job:id ────►│                  │
   │                      │── lpush job_list ──►│                  │
   │                      │── apply_async ─────────────────────►  │
   │◄─ {job_id} ──────────│                   │                  │
   │                      │                   │                  │
   │── GET /jobs/id/logs ─►│                   │                  │
   │   (SSE stream)       │── subscribe ──────►│                  │
   │                      │                   │  run script      │
   │                      │                   │◄── publish log ──│
   │◄─ data: "log line" ──│◄── message ───────│                  │
   │◄─ data: "log line" ──│◄── message ───────│                  │
   │◄─ data: __DONE__ ────│◄── message ───────│                  │
   │                      │                   │                  │
   │── GET /jobs/id/files ►│                   │                  │
   │◄─ [{name, is_image}] ─│                   │                  │
   │── GET /jobs/id/files/ ►│                   │                  │
   │◄─ (PNG binary) ───── │                   │                  │
```

---

## Features

- **Monaco Editor** — VSCode's editor engine with Python syntax highlighting
- **Live terminal output** — Server-Sent Events (SSE) stream logs in real time
- **In-browser results viewer** — PNG plots displayed without downloading
- **Job queue** — `concurrency=1` ensures hardware is accessed one job at a time
- **Template library** — Example SpiNNaker2 scripts in the sidebar
- **Save & load codes** — Per-user code snippets stored in SQLite
- **User authentication** — JWT-based auth with bcrypt password hashing
- **Rate limiting** — Login brute-force protection and per-user job limits
- **Sandboxed execution** — CPU time, memory, and file size limits per job
- **Stop running jobs** — Kill a running job instantly from the browser
- **Tailscale access** — Secure network-level isolation, no open ports required

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Vanilla HTML/JS + Monaco Editor (CDN) |
| API | FastAPI + Uvicorn |
| Task queue | Celery |
| Broker / cache | Redis (Valkey on Fedora) |
| Database | SQLite (via SQLAlchemy async) |
| Execution | subprocess + conda `spinnaker2` env |
| Auth | JWT (`python-jose`) + bcrypt (`passlib`) |
| Process manager | systemd |
| Network | Tailscale VPN |

---

## Project Structure

```
spinnaker-web/
├── main.py              # FastAPI app — all API endpoints
├── auth.py              # JWT authentication helpers
├── models.py            # SQLAlchemy models (User, SavedCode)
├── database.py          # Async SQLite connection
├── celery_app.py        # Celery configuration
├── tasks.py             # Celery task: run_script
├── sandbox.py           # Sandboxed subprocess execution
│
├── static/
│   └── index.html       # Single-page frontend (Monaco + SSE + viewer)
│
├── templates/           # Example SpiNNaker2 scripts shown in sidebar
│   ├── 01_hello_spinnaker.py
│   ├── 02_lif_neuron.py
│   └── 03_rate_coding.py
│
├── uploads/             # Uploaded scripts (gitignored)
├── results/             # Output tar.gz archives (gitignored)
├── job_outputs/         # Per-job working directories (gitignored)
│
├── setup.sh             # One-command Fedora installer
├── deploy.sh            # rsync + remote setup from local machine
├── start.sh             # Start services (systemd or manual)
├── stop.sh              # Stop services
├── status.sh            # Show service status
│
├── requirements.txt
├── .env.example         # Configuration template
└── .gitignore
```

---

## Installation (Fedora)

### Option A — Deploy from local machine
```bash
bash deploy.sh                        # uses geb@100.104.85.76 by default
bash deploy.sh geb@192.168.1.195      # or specify address
```

### Option B — Run directly on Fedora
```bash
git clone https://github.com/theprelior/spinnaker-web.git
cd spinnaker-web
bash setup.sh
```

`setup.sh` automatically:
1. Installs Redis (`dnf install redis`)
2. Creates a Python virtualenv and installs dependencies
3. Generates a random JWT secret in `.env`
4. Detects the `spinnaker2` conda environment
5. Copies `.env` to `/etc/spinnaker-web.env` (SELinux-safe location)
6. Sets SELinux context on `.venv/bin/`
7. Registers and starts `spinnaker-web` and `spinnaker-celery` systemd services

---

## Configuration

Copy `.env.example` to `.env` and adjust:

```env
JWT_SECRET=<random 64-char hex>      # python3 -c "import secrets; print(secrets.token_hex(32))"
ALLOW_REGISTRATION=true              # set false after creating accounts
SPINNAKER_PYTHON=/home/geb/.conda/envs/spinnaker2/bin/python3
MAX_JOBS_PER_HOUR=999
JOB_TIMEOUT_SECONDS=300
JOB_MEMORY_MB=512
```

After changing `.env` on Fedora:
```bash
sudo cp .env /etc/spinnaker-web.env
sudo systemctl restart spinnaker-web spinnaker-celery
```

---

## Usage

### Writing & Running Code
1. Open `http://100.104.85.76:8000` in your browser
2. Write Python in the editor (or load a template from the sidebar)
3. Press **▶ Run** (or `Ctrl+Enter`)
4. Watch live output in the terminal panel
5. When done: **⊞ View** to see plots in-browser, **↓ Download** for all output files

### Plotting
Scripts run headless — use `savefig` instead of `show`:
```python
import matplotlib
matplotlib.use('Agg')          # already set by the server
fig.savefig("my_plot.png")     # saved to job output directory
# plt.show()                   # ← has no effect, skip it
```

### Saving Code
- **💾 Save** (or `Ctrl+S`) to save the current editor content
- Saved codes appear in the sidebar under *Saved Codes*

---

## Development Workflow

```bash
# 1. Make changes locally
# 2. Test on local machine (if Redis + Celery running)

# 3. Push to GitHub
git add .
git commit -m "feat: ..."
git push

# 4. Update Fedora
ssh geb@100.104.85.76
cd ~/Desktop/spinnaker-web
git pull
sudo systemctl restart spinnaker-web spinnaker-celery
```

---

## Service Management (Fedora)

```bash
# Status
sudo systemctl status spinnaker-web spinnaker-celery

# Restart
sudo systemctl restart spinnaker-web spinnaker-celery

# Logs
sudo journalctl -u spinnaker-web   -f    # FastAPI
sudo journalctl -u spinnaker-celery -f   # Celery worker

# Stop
sudo systemctl stop spinnaker-web spinnaker-celery
```

---

## Security

| Layer | Mechanism |
|-------|-----------|
| Network | Tailscale VPN — server not reachable from public internet |
| Auth | JWT tokens (24h expiry) + bcrypt password hashing |
| Brute force | IP-based login rate limit: 5 attempts / 15 min |
| Job rate | Per-user limit configurable via `MAX_JOBS_PER_HOUR` |
| Execution | CPU time limit, RAM limit, file size limit, wall-clock timeout |
| Isolation | Stripped environment variables, job-specific working directory |
| Headers | `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy` |
| Secrets | `.env` stored at `/etc/spinnaker-web.env` (SELinux-safe, mode 600) |

> **Note:** For maximum isolation, consider wrapping the subprocess in a container (Docker/Podman). The current sandbox is appropriate for a trusted-user Tailscale deployment.

---

## Access

| Method | URL |
|--------|-----|
| Local | `http://localhost:8000` |
| Local network | `http://192.168.1.195:8000` |
| Tailscale | `http://100.104.85.76:8000` |
