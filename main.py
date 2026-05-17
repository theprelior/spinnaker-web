import json
import os
import signal
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

redis_async: aioredis.Redis = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_async
    await init_db()
    redis_async = aioredis.from_url("redis://localhost:6379", decode_responses=True)
    yield
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
        "created_at": current_user.created_at.isoformat(),
    }


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
        "status":   "queued",
        "filename": file.filename,
        "user_id":  str(current_user.id),
        "queued_at": datetime.utcnow().isoformat(),
    })
    await redis_async.lpush(f"job_list:{current_user.id}", job_id)

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
async def stream_logs(job_id: str, current_user: User = Depends(get_current_user)):
    await _owned_job(job_id, current_user)

    async def generate():
        existing = await redis_async.lrange(f"logs:{job_id}", 0, -1)
        for line in existing:
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

    # Önce stop_requested flag'ini set et (sandbox loop okur)
    await redis_async.hset(f"job:{job_id}", "stop_requested", "1")

    # PID varsa process grubunu direkt öldür
    pid_str = data.get("pid")
    if pid_str:
        try:
            os.killpg(os.getpgid(int(pid_str)), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    await redis_async.hset(f"job:{job_id}", "status", "failed")
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
