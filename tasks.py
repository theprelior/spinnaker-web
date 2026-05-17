import tarfile
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


@celery.task(bind=True)
def run_script(self, job_id: str, script_path: str, user_id: int) -> None:
    redis_client.hset(f"job:{job_id}", mapping={"status": "running", "user_id": str(user_id)})

    work_dir = JOB_OUTPUT_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    returncode = run_sandboxed(job_id, script_path, work_dir, lambda line: _pub(job_id, line))

    # Çıktı dosyalarını paketle
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
        "status": status,
        "returncode": str(returncode),
    })
    _pub(job_id, f"[Process exited with code {returncode}]")
    _pub(job_id, "__DONE__")
