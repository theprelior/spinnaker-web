import asyncio
import json
import os
import secrets
import signal
import subprocess as _subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, field_validator
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.datastructures import MutableHeaders
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from auth import create_token, get_current_user, hash_password, verify_password
from database import get_db, init_db
from models import SavedCode, User

load_dotenv()

UPLOAD_DIR  = Path("uploads")
RESULTS_DIR = Path("results")
TEMPLATE_DIR = Path("templates")
for d in (UPLOAD_DIR, RESULTS_DIR, TEMPLATE_DIR):
    d.mkdir(exist_ok=True)

ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "true").lower() == "true"
MAX_JOBS_PER_HOUR  = int(os.getenv("MAX_JOBS_PER_HOUR", "10"))
MAX_UPLOAD_BYTES   = 1 * 1024 * 1024   # 1 MB
ADMIN_USERS        = {u.strip() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()}

# Management board IP (STM at 192.168.1.2) for keepalive + status checks
_MGMT_IP            = os.getenv("BOARD_MANAGEMENT_IP", "192.168.1.2")
_MGMT_PORT          = int(os.getenv("BOARD_MANAGEMENT_PORT", "22"))  # SSH or any known port
_KEEPALIVE_INTERVAL = int(os.getenv("BOARD_KEEPALIVE_INTERVAL", "120"))  # seconds

redis_async: aioredis.Redis = None


async def _tcp_reachable(ip: str, port: int, timeout: float = 2.0) -> bool:
    """Return True if host:port accepts a TCP connection (or sends RST = host alive)."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except asyncio.TimeoutError:
        return False
    except OSError as e:
        # ECONNREFUSED (111) means host is UP but port is closed — still alive
        import errno
        return e.errno == errno.ECONNREFUSED


async def _board_keepalive_loop() -> None:
    """Touch management board every BOARD_KEEPALIVE_INTERVAL seconds when idle."""
    if not _MGMT_IP:
        return
    while True:
        await asyncio.sleep(_KEEPALIVE_INTERVAL)
        try:
            if await redis_async.exists("hw_running"):
                continue
            await _tcp_reachable(_MGMT_IP, _MGMT_PORT)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_async
    await init_db()
    redis_async = aioredis.from_url("redis://localhost:6379", decode_responses=True)

    # Self-heal: if hw_running points to a job that was mid-run when the worker
    # restarted, that job will never complete on its own — mark it failed now.
    stale_id = await redis_async.get("hw_running")
    if stale_id:
        job_data = await redis_async.hgetall(f"job:{stale_id}")
        if job_data and job_data.get("status") == "running":
            await redis_async.hset(f"job:{stale_id}", mapping={
                "status":       "failed",
                "completed_at": datetime.utcnow().isoformat(),
            })
            await redis_async.delete("hw_running")
            await redis_async.publish("queue_channel", "update")

    keepalive_task = asyncio.create_task(_board_keepalive_loop())
    yield
    keepalive_task.cancel()
    await redis_async.aclose()


class SecurityHeadersMiddleware:
    """Streaming response ile uyumlu ASGI security header middleware."""
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"]         = "DENY"
                headers["X-XSS-Protection"]        = "1; mode=block"
                headers["Referrer-Policy"]          = "strict-origin-when-cross-origin"
            await send(message)

        await self.app(scope, receive, send_with_headers)


app = FastAPI(title="SpiNNaker2 Playground", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(SecurityHeadersMiddleware)  # type: ignore[arg-type]


# ── Rate limiting helpers (Redis) ─────────────────────────────────────────────

async def _check_login_rate(ip: str) -> None:
    key = f"rl:login:{ip}"
    attempts = await redis_async.incr(key)
    if attempts == 1:
        await redis_async.expire(key, 900)   # 15 dakika pencere
    if attempts > 5:
        ttl = await redis_async.ttl(key)
        raise HTTPException(status_code=429, detail=f"Too many failed login attempts. Try again in {ttl}s.")


async def _reset_login_rate(ip: str) -> None:
    await redis_async.delete(f"rl:login:{ip}")


async def _check_job_rate(user_id: int) -> None:
    key = f"rl:jobs:{user_id}"
    count = await redis_async.incr(key)
    if count == 1:
        await redis_async.expire(key, 3600)  # saatlik pencere
    if count > MAX_JOBS_PER_HOUR:
        raise HTTPException(status_code=429, detail=f"Hourly job limit reached ({MAX_JOBS_PER_HOUR}/hour).")


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class RegisterIn(BaseModel):
    username: str
    email:    EmailStr
    password: str

    @field_validator("username")
    @classmethod
    def username_valid(cls, v: str) -> str:
        v = v.strip()
        if not 3 <= len(v) <= 50:
            raise ValueError("Username must be 3-50 characters")
        if not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Only letters, numbers, _ and - are allowed")
        return v

    @field_validator("password")
    @classmethod
    def password_valid(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class LoginIn(BaseModel):
    username: str
    password: str


class CodeIn(BaseModel):
    title: str
    code:  str

    @field_validator("title")
    @classmethod
    def title_valid(cls, v: str) -> str:
        v = v.strip()
        if not 1 <= len(v) <= 100:
            raise ValueError("Title must be 1-100 characters")
        return v


class CodeUpdateIn(BaseModel):
    title: str | None = None
    code:  str | None = None


class ShareIn(BaseModel):
    code:  str
    title: str = "untitled"

    @field_validator("code")
    @classmethod
    def code_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Code cannot be empty")
        if len(v) > 100_000:
            raise ValueError("Code too large (max 100 KB)")
        return v

    @field_validator("title")
    @classmethod
    def title_clean(cls, v: str) -> str:
        return v.strip()[:100] or "untitled"


# ── Auth endpoints ────────────────────────────────────────────────────────────

@app.post("/auth/register", status_code=201)
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db)):
    if not ALLOW_REGISTRATION:
        raise HTTPException(status_code=403, detail="Registration is currently closed.")

    dup = await db.execute(
        select(User).where((User.username == body.username) | (User.email == body.email))
    )
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="This username or email is already registered.")

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=hash_password(body.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"access_token": create_token(user.id, user.username), "token_type": "bearer"}


@app.post("/auth/login")
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    ip = request.client.host
    await _check_login_rate(ip)

    result = await db.execute(select(User).where(User.username == body.username, User.is_active == True))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    await _reset_login_rate(ip)
    return {"access_token": create_token(user.id, user.username), "token_type": "bearer"}


@app.get("/auth/me")
async def me(current_user: User = Depends(get_current_user)):
    return {
        "id":         current_user.id,
        "username":   current_user.username,
        "email":      current_user.email,
        "is_admin":   current_user.username in ADMIN_USERS,
        "created_at": current_user.created_at.isoformat(),
    }


async def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.username not in ADMIN_USERS:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return current_user


# ── Template endpoints ────────────────────────────────────────────────────────

@app.get("/templates")
async def list_templates(_: User = Depends(get_current_user)):
    templates = []
    for f in sorted(TEMPLATE_DIR.glob("*.py")):
        first_line = f.read_text(encoding="utf-8").splitlines()[0].lstrip("#").strip()
        templates.append({"name": f.name, "description": first_line})
    return templates


@app.get("/templates/{name}")
async def get_template(name: str, _: User = Depends(get_current_user)):
    # Path traversal koruması
    safe_path = (TEMPLATE_DIR / name).resolve()
    if not str(safe_path).startswith(str(TEMPLATE_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Geçersiz dosya adı")
    if not safe_path.exists() or safe_path.suffix != ".py":
        raise HTTPException(status_code=404, detail="Şablon bulunamadı")
    return {"name": name, "code": safe_path.read_text(encoding="utf-8")}


# ── Job endpoints ─────────────────────────────────────────────────────────────

@app.post("/jobs", status_code=201)
async def submit_job(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    if not file.filename.endswith(".py"):
        raise HTTPException(status_code=400, detail="Only .py files are accepted.")

    await _check_job_rate(current_user.id)

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Script 1 MB'ı geçemez.")

    job_id = str(uuid.uuid4())
    script_path = (UPLOAD_DIR / f"{job_id}.py").resolve()
    script_path.write_bytes(content)

    await redis_async.hset(f"job:{job_id}", mapping={
        "status":    "queued",
        "filename":  file.filename,
        "user_id":   str(current_user.id),
        "username":  current_user.username,
        "email":     current_user.email or "",
        "queued_at": datetime.utcnow().isoformat(),
    })
    await redis_async.lpush(f"job_list:{current_user.id}", job_id)
    await redis_async.rpush("hw_queue", job_id)   # hardware scheduler queue

    from tasks import run_script
    task = run_script.apply_async(args=[job_id, str(script_path), current_user.id])
    await redis_async.hset(f"job:{job_id}", "task_id", task.id)

    return {"job_id": job_id}


@app.get("/jobs")
async def list_jobs(current_user: User = Depends(get_current_user)):
    job_ids = await redis_async.lrange(f"job_list:{current_user.id}", 0, 49)
    jobs = []
    for jid in job_ids:
        data = await redis_async.hgetall(f"job:{jid}")
        if data:
            jobs.append({"job_id": jid, **data})
    return jobs


async def _owned_job(job_id: str, current_user: User) -> dict:
    data = await redis_async.hgetall(f"job:{job_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Job not found.")
    if data.get("user_id") != str(current_user.id):
        raise HTTPException(status_code=403, detail="Access denied.")
    return data


@app.get("/jobs/{job_id}/status")
async def job_status(job_id: str, current_user: User = Depends(get_current_user)):
    data = await _owned_job(job_id, current_user)
    return {"job_id": job_id, **data}


@app.get("/jobs/{job_id}/logs")
async def stream_logs(
    job_id: str,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
):
    await _owned_job(job_id, current_user)

    async def generate():
        existing = await redis_async.lrange(f"logs:{job_id}", 0, -1)
        for line in existing[max(0, offset):]:
            if line == "__DONE__":
                yield "data: __DONE__\n\n"
                return
            yield f"data: {json.dumps(line)}\n\n"

        status_val = await redis_async.hget(f"job:{job_id}", "status")
        if status_val in ("done", "failed", "error", "timeout"):
            yield "data: __DONE__\n\n"
            return

        pubsub = redis_async.pubsub()
        await pubsub.subscribe(f"logs_channel:{job_id}")
        try:
            async for msg in pubsub.listen():
                if msg["type"] == "message":
                    data = msg["data"]
                    if data == "__DONE__":
                        yield "data: __DONE__\n\n"
                        break
                    yield f"data: {json.dumps(data)}\n\n"
        finally:
            await pubsub.unsubscribe(f"logs_channel:{job_id}")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str, current_user: User = Depends(get_current_user)):
    data = await _owned_job(job_id, current_user)
    if data.get("status") not in ("running", "queued"):
        raise HTTPException(status_code=400, detail="Job has already completed.")

    await redis_async.hset(f"job:{job_id}", "stop_requested", "1")

    slurm_job_id = data.get("slurm_job_id")
    if slurm_job_id:
        try:
            _subprocess.run(["scancel", slurm_job_id], capture_output=True)
        except Exception:
            pass
    elif data.get("pid"):
        try:
            os.killpg(os.getpgid(int(data["pid"])), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    # Clean up hardware queue
    await redis_async.lrem("hw_queue", 0, job_id)
    running = await redis_async.get("hw_running")
    if running == job_id:
        await redis_async.delete("hw_running")
        await redis_async.publish("queue_channel", "update")

    await redis_async.hset(f"job:{job_id}", mapping={
        "status":       "failed",
        "completed_at": datetime.utcnow().isoformat(),
    })
    return {"ok": True}


@app.get("/queue")
async def get_queue(current_user: User = Depends(get_current_user)):
    running_id = await redis_async.get("hw_running")
    queue_ids  = await redis_async.lrange("hw_queue", 0, -1)

    # Compute ETA from rolling average duration
    durations = await redis_async.lrange("hw_durations", 0, -1)
    avg_s = (sum(float(d) for d in durations) / len(durations)) if durations else None

    running = None
    if running_id:
        job_data = await redis_async.hgetall(f"job:{running_id}")
        if job_data and job_data.get("status") == "running":
            running = {
                "job_id":     running_id,
                "username":   job_data.get("username", "—"),
                "filename":   job_data.get("filename", "—"),
                "started_at": job_data.get("started_at"),
                "is_mine":    job_data.get("user_id") == str(current_user.id),
            }
        else:
            await redis_async.delete("hw_running")  # stale key — self-heal

    # If no web job is tracked, check Slurm directly for SSH admin jobs
    if not running:
        try:
            proc = await asyncio.create_subprocess_exec(
                "squeue", "-h", "-o", "%T|%u|%j",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            for line in stdout.decode().strip().splitlines():
                parts = line.split("|")
                if parts and parts[0].strip() == "RUNNING":
                    running = {
                        "job_id":     None,
                        "username":   parts[1].strip() if len(parts) > 1 else "admin",
                        "filename":   parts[2].strip() if len(parts) > 2 else "SSH job",
                        "started_at": None,
                        "is_mine":    False,
                    }
                    break
        except Exception:
            pass

    queue = []
    for i, jid in enumerate(queue_ids):
        job_data = await redis_async.hgetall(f"job:{jid}")
        if job_data and job_data.get("status") == "queued":
            eta_s = round(avg_s * (i + 1)) if avg_s else None
            queue.append({
                "job_id":    jid,
                "position":  i + 1,
                "username":  job_data.get("username", "—"),
                "filename":  job_data.get("filename", "—"),
                "queued_at": job_data.get("queued_at"),
                "is_mine":   job_data.get("user_id") == str(current_user.id),
                "eta_s":     eta_s,
            })

    return {
        "running":        running,
        "queue":          queue,
        "queue_length":   len(queue),
        "avg_duration_s": round(avg_s) if avg_s else None,
    }


@app.get("/queue/stats")
async def queue_stats(_: User = Depends(get_current_user)):
    now_ts  = datetime.utcnow().timestamp()
    since   = now_ts - 86400
    entries = await redis_async.zrangebyscore("hw_timeline", since, "+inf", withscores=True)

    hours_minutes = [0.0] * 24   # index 0 = oldest, 23 = most recent hour
    hours_count   = [0]   * 24

    for entry, score in entries:
        try:
            data      = json.loads(entry)
            hours_ago = (now_ts - score) / 3600
            bucket    = min(23, int(hours_ago))     # 0 = within last hour
            idx       = 23 - bucket                 # flip: index 23 = most recent
            hours_minutes[idx] += data.get("duration_s", 0) / 60
            hours_count[idx]   += 1
        except Exception:
            pass

    total_minutes = sum(hours_minutes)
    return {
        "hours_busy_minutes": hours_minutes,
        "hours_job_count":    hours_count,
        "total_jobs_24h":     len(entries),
        "total_minutes_24h":  round(total_minutes, 1),
        "utilization_pct":    round(total_minutes / (24 * 60) * 100, 1),
    }


@app.get("/jobs/quota")
async def job_quota(current_user: User = Depends(get_current_user)):
    key  = f"rl:jobs:{current_user.id}"
    used = int(await redis_async.get(key) or 0)
    ttl  = await redis_async.ttl(key)
    return {
        "used":       min(used, MAX_JOBS_PER_HOUR),
        "limit":      MAX_JOBS_PER_HOUR,
        "resets_in":  max(0, ttl),
    }


@app.get("/health")
async def health():
    checks: dict = {"status": "ok", "redis": False, "hw_running": None}
    try:
        await redis_async.ping()
        checks["redis"] = True
        checks["hw_running"] = await redis_async.get("hw_running")
        queue_len = await redis_async.llen("hw_queue")
        checks["queue_length"] = queue_len
    except Exception as e:
        checks["status"] = "degraded"
        checks["error"]  = str(e)
    return checks


async def _slurm_hw_status() -> str:
    """Return 'online', 'busy', or 'offline' from Slurm node state."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "sinfo", "-n", "fedora", "-h", "-o", "%T",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        state = stdout.decode().strip().lower()
        if state in ("allocated", "mixed", "completing"):
            return "busy"
        if state == "idle":
            # Check SpinnmanSession lock files in SPINNAKER_DIR
            spinnaker_dir = Path(os.getenv("SPINNAKER_DIR", "/mnt/spinnaker"))
            if any(spinnaker_dir.glob("*.lock")):
                return "busy"
            return "online"
        return "offline"
    except Exception:
        return "offline"


@app.get("/hardware/ping")
async def hardware_ping(_: User = Depends(get_current_user)):
    status = await _slurm_hw_status()
    return {"online": status != "offline", "status": status}


# ── Auth config ───────────────────────────────────────────────────────────────

@app.get("/auth/config")
async def auth_config():
    return {"allow_registration": ALLOW_REGISTRATION}


# ── User stats ────────────────────────────────────────────────────────────────

@app.get("/users/me/stats")
async def user_stats(current_user: User = Depends(get_current_user)):
    job_ids = await redis_async.lrange(f"job_list:{current_user.id}", 0, -1)
    done = 0; failed_c = 0; total_dur = 0.0
    for jid in job_ids:
        d = await redis_async.hgetall(f"job:{jid}")
        s = d.get("status", "")
        if s == "done":
            done += 1
        elif s in ("failed", "timeout"):
            failed_c += 1
        try:
            total_dur += float(d.get("duration_s", 0))
        except ValueError:
            pass
    return {
        "total":            len(job_ids),
        "done":             done,
        "failed":           failed_c,
        "total_duration_s": round(total_dur, 1),
        "avg_duration_s":   round(total_dur / done, 1) if done else None,
    }


# ── Share ─────────────────────────────────────────────────────────────────────

@app.post("/share", status_code=201)
async def create_share(body: ShareIn, current_user: User = Depends(get_current_user)):
    share_id = secrets.token_urlsafe(8)
    data = json.dumps({
        "code":       body.code,
        "title":      body.title,
        "username":   current_user.username,
        "created_at": datetime.utcnow().isoformat(),
    })
    await redis_async.setex(f"share:{share_id}", 86400 * 30, data)
    return {"share_id": share_id}


@app.get("/share/{share_id}")
async def get_share(share_id: str):
    raw = await redis_async.get(f"share:{share_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Share link not found or expired.")
    return json.loads(raw)


# ── Admin endpoints ───────────────────────────────────────────────────────────

@app.get("/admin/queue")
async def admin_queue(_: User = Depends(_require_admin)):
    running_id = await redis_async.get("hw_running")
    queue_ids  = await redis_async.lrange("hw_queue", 0, -1)

    running = None
    if running_id:
        d = await redis_async.hgetall(f"job:{running_id}")
        if d and d.get("status") == "running":
            running = {"job_id": running_id, **d}
        else:
            await redis_async.delete("hw_running")

    queue = []
    for i, jid in enumerate(queue_ids):
        d = await redis_async.hgetall(f"job:{jid}")
        if d and d.get("status") == "queued":
            queue.append({"job_id": jid, "position": i + 1, **d})

    return {"running": running, "queue": queue}


@app.get("/admin/users")
async def admin_users(_: User = Depends(_require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users  = result.scalars().all()
    return [
        {
            "id":         u.id,
            "username":   u.username,
            "email":      u.email,
            "is_active":  u.is_active,
            "is_admin":   u.username in ADMIN_USERS,
            "created_at": u.created_at.isoformat(),
        }
        for u in users
    ]


@app.post("/admin/users/{user_id}/toggle")
async def admin_toggle_user(
    user_id: int,
    _: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user   = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    user.is_active = not user.is_active
    await db.commit()
    return {"id": user.id, "username": user.username, "is_active": user.is_active}


@app.post("/admin/jobs/{job_id}/kill")
async def admin_kill_job(job_id: str, _: User = Depends(_require_admin)):
    data = await redis_async.hgetall(f"job:{job_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Job not found.")
    if data.get("status") not in ("running", "queued"):
        raise HTTPException(status_code=400, detail="Job already completed.")

    await redis_async.hset(f"job:{job_id}", "stop_requested", "1")

    slurm_job_id = data.get("slurm_job_id")
    if slurm_job_id:
        try:
            _subprocess.run(["scancel", slurm_job_id], capture_output=True)
        except Exception:
            pass
    elif data.get("pid"):
        try:
            os.killpg(os.getpgid(int(data["pid"])), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    await redis_async.lrem("hw_queue", 0, job_id)
    running = await redis_async.get("hw_running")
    if running == job_id:
        await redis_async.delete("hw_running")
        await redis_async.publish("queue_channel", "update")

    await redis_async.hset(f"job:{job_id}", mapping={
        "status":       "failed",
        "completed_at": datetime.utcnow().isoformat(),
    })
    return {"ok": True, "job_id": job_id}


def _capture_env_info_sync() -> dict:
    """Capture spinnaker2fresh conda env metadata (runs in thread executor)."""
    python = os.getenv("SPINNAKER_PYTHON", sys.executable)
    info: dict = {"executable": python, "captured_at": datetime.utcnow().isoformat()}

    try:
        r = _subprocess.run([python, "-c", "import sys; print(sys.version)"],
                            capture_output=True, text=True, timeout=5)
        info["python_version"] = r.stdout.strip()
    except Exception as e:
        info["python_version"] = f"error: {e}"

    try:
        r = _subprocess.run([python, "-m", "pip", "list", "--format=json"],
                            capture_output=True, text=True, timeout=30)
        all_pkgs = json.loads(r.stdout)
        KEEP = {
            "spinnaker2", "py-spinnaker2", "pyspinnaker2",
            "numpy", "scipy", "matplotlib", "pandas",
            "brian2", "pynn", "nir", "torch", "tensorflow", "networkx",
        }
        keep_norm = {k.lower().replace("-", "_") for k in KEEP}
        info["key_packages"]   = [p for p in all_pkgs
                                  if p["name"].lower().replace("-", "_") in keep_norm]
        info["total_packages"] = len(all_pkgs)
    except Exception as e:
        info["key_packages"]    = []
        info["packages_error"]  = str(e)

    return info


@app.get("/env/info")
async def env_info(_: User = Depends(get_current_user)):
    cached = await redis_async.get("hw_env_info")
    if cached:
        return json.loads(cached)
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _capture_env_info_sync)
    await redis_async.setex("hw_env_info", 86400, json.dumps(info))
    return info


@app.delete("/env/info/cache")
async def clear_env_cache(_: User = Depends(_require_admin)):
    await redis_async.delete("hw_env_info")
    return {"ok": True}


@app.get("/jobs/{job_id}/results")
async def download_results(job_id: str, current_user: User = Depends(get_current_user)):
    data = await _owned_job(job_id, current_user)
    result_path = RESULTS_DIR / f"{job_id}.tar.gz"
    if not result_path.exists():
        raise HTTPException(status_code=404, detail="Results are not ready yet.")
    base = data.get("filename", "script").replace(".py", "")
    return FileResponse(result_path, media_type="application/gzip",
                        filename=f"results_{base}_{job_id[:8]}.tar.gz")


@app.get("/jobs/{job_id}/files")
async def list_job_files(job_id: str, current_user: User = Depends(get_current_user)):
    await _owned_job(job_id, current_user)
    base = Path("job_outputs") / job_id
    search = base / "results" if (base / "results").exists() else base
    if not search.exists():
        return []
    IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg"}
    TEXT_EXT  = {".txt", ".csv", ".log", ".json"}
    files = []
    for f in sorted(search.rglob("*")):
        if f.is_file():
            ext = f.suffix.lower()
            files.append({
                "name":     f.name,
                "path":     str(f.relative_to(base)),
                "size":     f.stat().st_size,
                "is_image": ext in IMAGE_EXT,
                "is_text":  ext in TEXT_EXT,
            })
    return files


@app.get("/jobs/{job_id}/files/{file_path:path}")
async def get_job_file(job_id: str, file_path: str, current_user: User = Depends(get_current_user)):
    await _owned_job(job_id, current_user)
    base   = (Path("job_outputs") / job_id).resolve()
    target = (base / file_path).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=400, detail="Invalid path.")
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(target)


# ── Saved code endpoints ──────────────────────────────────────────────────────

@app.get("/codes")
async def list_codes(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SavedCode).where(SavedCode.user_id == current_user.id)
                         .order_by(SavedCode.updated_at.desc())
    )
    codes = result.scalars().all()
    return [{"id": c.id, "title": c.title, "updated_at": c.updated_at.isoformat()} for c in codes]


@app.post("/codes", status_code=201)
async def save_code(body: CodeIn, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    code = SavedCode(user_id=current_user.id, title=body.title, code=body.code)
    db.add(code)
    await db.commit()
    await db.refresh(code)
    return {"id": code.id, "title": code.title}


@app.get("/codes/{code_id}")
async def get_code(code_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SavedCode).where(SavedCode.id == code_id, SavedCode.user_id == current_user.id))
    code = result.scalar_one_or_none()
    if not code:
        raise HTTPException(status_code=404, detail="Code not found.")
    return {"id": code.id, "title": code.title, "code": code.code}


@app.put("/codes/{code_id}")
async def update_code(code_id: int, body: CodeUpdateIn, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SavedCode).where(SavedCode.id == code_id, SavedCode.user_id == current_user.id))
    code = result.scalar_one_or_none()
    if not code:
        raise HTTPException(status_code=404, detail="Code not found.")
    if body.title is not None:
        code.title = body.title
    if body.code is not None:
        code.code = body.code
    code.updated_at = datetime.utcnow()
    await db.commit()
    return {"id": code.id, "title": code.title}


@app.delete("/codes/{code_id}", status_code=204)
async def delete_code(code_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SavedCode).where(SavedCode.id == code_id, SavedCode.user_id == current_user.id))
    code = result.scalar_one_or_none()
    if not code:
        raise HTTPException(status_code=404, detail="Code not found.")
    await db.delete(code)
    await db.commit()


# ── Static files ──────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")
