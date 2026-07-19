import json
import os
import smtplib
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

import redis

from celery_app import celery

redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

RESULTS_DIR    = Path("results")
JOB_OUTPUT_DIR = Path("job_outputs")
RESULTS_DIR.mkdir(exist_ok=True)
JOB_OUTPUT_DIR.mkdir(exist_ok=True)

SLURM_WRAPPER     = "/opt/slurm-wrappers/wrapper.sh"
SLURM_OUTPUT_DIR  = Path("/var/log/spinn_jobs")

# Matplotlib / process preamble prepended to every user script.
# Forces headless backend and converts plt.show() calls to saved PNGs,
# matching the behaviour the web UI's sandbox previously provided.
_PREAMBLE = '''\
import matplotlib as _mpl; _mpl.use("Agg")
import matplotlib.pyplot as _plt
_fig_n = [0]
def _auto_show(*_a, **_kw):
    for _n in _plt.get_fignums():
        _fig_n[0] += 1
        _fname = f"figure_{_fig_n[0]}.png"
        _plt.figure(_n).savefig(_fname, dpi=100, bbox_inches="tight")
        print(f"[Plot saved: {_fname}]")
    _plt.close("all")
_plt.show = _auto_show
'''


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


def _clear_spinnaker_locks() -> None:
    # Python-level ETH lock dosyalarını temizle (crash'te /tmp'de kalır)
    # NOT: /mnt/spinnaker/locks/ C++ POSIX lock'larına dokunma —
    #      process ölünce OS otomatik serbest bırakır, dosyaların var olması gerekir.
    lock_dir = Path(os.getenv("SPINNAKER_LOCK_PATH", "/tmp"))
    for lock_file in lock_dir.glob("s2_eth_lock_*"):
        try:
            lock_file.unlink()
        except Exception:
            pass


def _update_avg_duration(seconds: float) -> None:
    """Rolling average of last 20 job durations (for ETA estimates)."""
    redis_client.lpush("hw_durations", f"{seconds:.1f}")
    redis_client.ltrim("hw_durations", 0, 19)


def _slurm_state(slurm_job_id: str) -> tuple[str, int]:
    """Return (state, exit_code) from sacct, or ("UNKNOWN", -1) if not ready yet."""
    result = subprocess.run(
        ["sacct", "-j", slurm_job_id, "-X", "--noheader", "--parsable2",
         "-o", "State,ExitCode"],
        capture_output=True, text=True
    )
    for line in result.stdout.strip().splitlines():
        parts = line.split("|")
        if not parts:
            continue
        state = parts[0].strip()
        exit_str = parts[1].strip() if len(parts) > 1 else "0:0"
        if state in ("COMPLETED", "FAILED", "TIMEOUT", "CANCELLED",
                     "NODE_FAIL", "OUT_OF_MEMORY"):
            try:
                code = int(exit_str.split(":")[0])
            except ValueError:
                code = -1
            return state, code
        if state in ("PENDING", "RUNNING", "COMPLETING", "SUSPENDED"):
            return state, -1
    return "UNKNOWN", -1


@celery.task(bind=True)
def run_script(self, job_id: str, script_path: str, user_id: int) -> None:
    started_at = datetime.utcnow().isoformat()

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

    _clear_spinnaker_locks()

    # Preamble + user code → temp file that wrapper.sh will execute.
    try:
        original = Path(script_path).read_text(encoding="utf-8", errors="replace")
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", dir=work_dir, delete=False, prefix="_run_"
        )
        tmp.write(_PREAMBLE + original)
        tmp.close()
        wrapped_path = tmp.name
    except Exception as exc:
        _pub(job_id, f"[Error preparing script: {exc}]")
        _pub(job_id, "__DONE__")
        redis_client.hset(f"job:{job_id}", mapping={
            "status":       "failed",
            "completed_at": datetime.utcnow().isoformat(),
        })
        redis_client.delete("hw_running")
        redis_client.publish("queue_channel", "update")
        return

    # Submit to Slurm.
    try:
        sbatch_result = subprocess.run(
            [
                "sbatch",
                "--gres=spinnaker:1",
                "--account=web",
                "--qos=webqos",
                f"--comment=webuser:{user_id}",
                f"--chdir={work_dir}",
                f"--output={SLURM_OUTPUT_DIR}/%j.out",
                f"--error={SLURM_OUTPUT_DIR}/%j.err",
                SLURM_WRAPPER,
                wrapped_path,
            ],
            capture_output=True, text=True, check=True
        )
        # "Submitted batch job 42" → "42"
        slurm_job_id = sbatch_result.stdout.strip().split()[-1]
        int(slurm_job_id)  # validate numeric
    except Exception as exc:
        _pub(job_id, f"[sbatch submission failed: {exc}]")
        _pub(job_id, "__DONE__")
        Path(wrapped_path).unlink(missing_ok=True)
        redis_client.hset(f"job:{job_id}", mapping={
            "status":       "failed",
            "completed_at": datetime.utcnow().isoformat(),
        })
        redis_client.delete("hw_running")
        redis_client.publish("queue_channel", "update")
        return

    # Store slurm_job_id in Redis so the /jobs/{id}/stop endpoint can scancel it.
    redis_client.hset(f"job:{job_id}", "slurm_job_id", slurm_job_id)
    slurm_out_file = SLURM_OUTPUT_DIR / f"{slurm_job_id}.out"

    # Stream the Slurm output file to Redis in a background thread.
    # tail -F waits for the file to appear (job may be PENDING) and follows writes.
    stop_tail = threading.Event()

    def _tail_output() -> None:
        # Wait up to 2 min for the output file to appear (generous for a slow laptop).
        for _ in range(60):
            if slurm_out_file.exists() or stop_tail.is_set():
                break
            time.sleep(2)
        if stop_tail.is_set():
            return
        try:
            tail = subprocess.Popen(
                ["tail", "-F", "-n", "+0", str(slurm_out_file)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
            for line in iter(tail.stdout.readline, ""):
                if stop_tail.is_set():
                    break
                _pub(job_id, line.rstrip("\n"))
            tail.kill()
            tail.wait()
        except Exception:
            pass

    tail_thread = threading.Thread(target=_tail_output, daemon=True)
    tail_thread.start()

    # Poll for job completion.
    # Slurm's webqos enforces MaxWall=00:30:00; this outer ceiling is a safety net.
    poll_ceiling = 7200  # 2 hours
    poll_start   = time.monotonic()
    final_state  = "UNKNOWN"
    returncode   = -2

    try:
        while True:
            if redis_client.hget(f"job:{job_id}", "stop_requested") == "1":
                subprocess.run(["scancel", slurm_job_id], capture_output=True)
                _pub(job_id, "[Stopped by user]")
                final_state = "CANCELLED_BY_USER"
                break

            state, code = _slurm_state(slurm_job_id)
            if state in ("COMPLETED", "FAILED", "TIMEOUT", "CANCELLED",
                         "NODE_FAIL", "OUT_OF_MEMORY"):
                final_state = state
                returncode  = code
                break

            if time.monotonic() - poll_start > poll_ceiling:
                subprocess.run(["scancel", slurm_job_id], capture_output=True)
                final_state = "TIMEOUT"
                break

            time.sleep(3)
    finally:
        stop_tail.set()
        tail_thread.join(timeout=5)
        Path(wrapped_path).unlink(missing_ok=True)
        redis_client.hdel(f"job:{job_id}", "slurm_job_id", "stop_requested")
        redis_client.delete("hw_running")
        redis_client.publish("queue_channel", "update")

    completed_at = datetime.utcnow()
    duration_s   = (completed_at - datetime.fromisoformat(started_at)).total_seconds()
    _update_avg_duration(duration_s)

    if final_state == "COMPLETED" and returncode == 0:
        status = "done"
    elif final_state in ("TIMEOUT", "OUT_OF_MEMORY"):
        status     = "timeout"
        returncode = -1
    elif final_state == "CANCELLED_BY_USER":
        status     = "failed"
        returncode = -3
    else:
        status = "failed"

    # Bundle any output files written to work_dir into a downloadable tarball.
    result_path = RESULTS_DIR / f"{job_id}.tar.gz"
    with tarfile.open(result_path, "w:gz") as tar:
        tar.add(work_dir, arcname="results")

    redis_client.hset(f"job:{job_id}", mapping={
        "status":       status,
        "returncode":   str(returncode),
        "completed_at": completed_at.isoformat(),
        "duration_s":   f"{duration_s:.1f}",
    })

    user_email = redis_client.hget(f"job:{job_id}", "email") or ""
    username   = redis_client.hget(f"job:{job_id}", "username") or "unknown"
    fname      = Path(script_path).name
    icon       = "✓" if status == "done" else "✗"

    _send_email(
        user_email,
        f"[SpiNNaker2] {icon} {fname} — {status}",
        f"Your SpiNNaker2 job has completed.\n\n"
        f"File:     {fname}\n"
        f"Status:   {status.upper()}\n"
        f"Duration: {duration_s:.0f}s\n\n"
        f"Log in to download your results.\n",
    )

    if status in ("failed", "timeout"):
        admin_email = os.getenv("ADMIN_EMAIL", "")
        logs        = redis_client.lrange(f"logs:{job_id}", 0, -1)
        log_tail    = "\n".join(logs[-50:])
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

    timeline_entry = json.dumps({"job_id": job_id, "duration_s": duration_s, "status": status})
    now_ts         = completed_at.timestamp()
    redis_client.zadd("hw_timeline", {timeline_entry: now_ts})
    redis_client.zremrangebyscore("hw_timeline", 0, now_ts - 86400 * 7)

    _pub(job_id, f"[Process exited with code {returncode}]")
    _pub(job_id, "__DONE__")
