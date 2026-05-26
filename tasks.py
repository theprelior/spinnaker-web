import json
import os
import smtplib
import tarfile
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

import redis

from celery_app import celery
from sandbox import run_sandboxed

redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

RESULTS_DIR    = Path("results")
JOB_OUTPUT_DIR = Path("job_outputs")
RESULTS_DIR.mkdir(exist_ok=True)
JOB_OUTPUT_DIR.mkdir(exist_ok=True)


def _pub(job_id: str, line: str) -> None:
    redis_client.rpush(f"logs:{job_id}", line)
    redis_client.publish(f"logs_channel:{job_id}", line)


def _send_email(to: str, subject: str, body: str) -> None:
    host = os.getenv("EMAIL_HOST", "")
    if not host or not to:
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = os.getenv("EMAIL_FROM", os.getenv("EMAIL_USER", "noreply@spinnaker2"))
        msg["To"]      = to
        port   = int(os.getenv("EMAIL_PORT", "587"))
        user   = os.getenv("EMAIL_USER", "")
        passwd = os.getenv("EMAIL_PASS", "")
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            if user:
                smtp.login(user, passwd)
            smtp.send_message(msg)
    except Exception as exc:
        print(f"[email] send failed: {exc}", flush=True)


def _update_avg_duration(seconds: float) -> None:
    """Rolling average of last 20 job durations (for ETA estimates)."""
    redis_client.lpush("hw_durations", f"{seconds:.1f}")
    redis_client.ltrim("hw_durations", 0, 19)


@celery.task(bind=True)
def run_script(self, job_id: str, script_path: str, user_id: int) -> None:
    started_at = datetime.utcnow().isoformat()

    # Claim the hardware slot and remove from visible queue
    redis_client.set("hw_running", job_id)
    redis_client.lrem("hw_queue", 0, job_id)
    redis_client.hset(f"job:{job_id}", mapping={
        "status":     "running",
        "user_id":    str(user_id),
        "started_at": started_at,
    })
    redis_client.publish("queue_channel", "update")

    work_dir = JOB_OUTPUT_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        returncode = run_sandboxed(job_id, script_path, work_dir, lambda line: _pub(job_id, line))

        completed_at = datetime.utcnow()
        duration_s = (completed_at - datetime.fromisoformat(started_at)).total_seconds()
        _update_avg_duration(duration_s)

        result_path = RESULTS_DIR / f"{job_id}.tar.gz"
        with tarfile.open(result_path, "w:gz") as tar:
            tar.add(work_dir, arcname="results")

        if returncode == 0:
            status = "done"
        elif returncode == -1:
            status = "timeout"
        else:
            status = "failed"

        redis_client.hset(f"job:{job_id}", mapping={
            "status":       status,
            "returncode":   str(returncode),
            "completed_at": completed_at.isoformat(),
            "duration_s":   f"{duration_s:.1f}",
        })

        user_email = redis_client.hget(f"job:{job_id}", "email") or ""
        username   = redis_client.hget(f"job:{job_id}", "username") or "unknown"
        fname = Path(script_path).name
        icon  = "✓" if status == "done" else "✗"

        # Kullanıcıya bildirim
        _send_email(
            user_email,
            f"[SpiNNaker2] {icon} {fname} — {status}",
            f"Your SpiNNaker2 job has completed.\n\n"
            f"File:     {fname}\n"
            f"Status:   {status.upper()}\n"
            f"Duration: {duration_s:.0f}s\n\n"
            f"Log in to download your results.\n",
        )

        # Hata varsa admin'e tam log ile bildir
        if status in ("failed", "timeout"):
            admin_email = os.getenv("ADMIN_EMAIL", "")
            logs = redis_client.lrange(f"logs:{job_id}", 0, -1)
            log_tail = "\n".join(logs[-50:])   # son 50 satır
            _send_email(
                admin_email,
                f"[SpiNNaker2 ADMIN] ✗ Job failed — {fname} ({username})",
                f"A job has failed on SpiNNaker2 Playground.\n\n"
                f"Job ID:   {job_id}\n"
                f"User:     {username} <{user_email}>\n"
                f"File:     {fname}\n"
                f"Status:   {status.upper()}\n"
                f"Duration: {duration_s:.0f}s\n\n"
                f"--- Last 50 log lines ---\n{log_tail}\n",
            )

        # Record in timeline for utilization chart (7-day window)
        timeline_entry = json.dumps({"job_id": job_id, "duration_s": duration_s, "status": status})
        now_ts = completed_at.timestamp()
        redis_client.zadd("hw_timeline", {timeline_entry: now_ts})
        redis_client.zremrangebyscore("hw_timeline", 0, now_ts - 86400 * 7)

        _pub(job_id, f"[Process exited with code {returncode}]")
        _pub(job_id, "__DONE__")

    except Exception as exc:
        redis_client.hset(f"job:{job_id}", mapping={
            "status":       "failed",
            "completed_at": datetime.utcnow().isoformat(),
        })
        _pub(job_id, f"[Internal worker error: {exc}]")
        _pub(job_id, "__DONE__")
        admin_email = os.getenv("ADMIN_EMAIL", "")
        _send_email(
            admin_email,
            f"[SpiNNaker2 ADMIN] ✗ Worker exception — {Path(script_path).name}",
            f"Unhandled exception in Celery worker.\n\n"
            f"Job ID: {job_id}\n"
            f"Error:  {exc}\n",
        )

    finally:
        redis_client.delete("hw_running")
        redis_client.publish("queue_channel", "update")
