"""
Güvenli script çalıştırma katmanı.

Korumalar:
  - CPU zaman limiti  (RLIMIT_CPU)
  - Bellek limiti     (RLIMIT_AS)
  - Dosya boyutu      (RLIMIT_FSIZE)
  - Process sayısı    (RLIMIT_NPROC) — fork bomb önleme
  - Açık dosya sayısı (RLIMIT_NOFILE)
  - Wall-clock timeout (subprocess.wait timeout + SIGKILL)
  - Temizlenmiş ortam değişkenleri (credential sızıntısı önleme)
  - Düşük öncelik     (nice +10)
"""

import os
import resource
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable

CPU_SECONDS  = int(os.getenv("JOB_TIMEOUT_SECONDS", "300"))
MEMORY_MB    = int(os.getenv("JOB_MEMORY_MB", "512"))
FILE_SIZE_MB = 100
MAX_PROCS    = 128
MAX_FDS      = 128
WALL_TIMEOUT = CPU_SECONDS + 30

# Conda ortamının Python'u — .env'de SPINNAKER_PYTHON ile ayarlanır
_PYTHON = os.getenv("SPINNAKER_PYTHON", sys.executable)

_conda_env = os.path.dirname(os.path.dirname(_PYTHON))   # .../envs/spinnaker2
_conda_bin = os.path.join(_conda_env, "bin")

SAFE_ENV: dict[str, str] = {
    "PATH":                    f"{_conda_bin}:/usr/local/bin:/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED":        "1",
    "LANG":                    "en_US.UTF-8",
    "HOME":                    "/tmp",
    "CONDA_PREFIX":            _conda_env,
    # Headless matplotlib (ekran yok)
    "MPLBACKEND":              "Agg",
    # OpenBLAS/numpy thread sayısını sınırla (paylaşımlı donanım)
    "OPENBLAS_NUM_THREADS":    "2",
    "OMP_NUM_THREADS":         "2",
    "MKL_NUM_THREADS":         "2",
}

_extra_pythonpath = os.getenv("SPINNAKER_PYTHONPATH", "")
if _extra_pythonpath:
    SAFE_ENV["PYTHONPATH"] = _extra_pythonpath


def _apply_limits() -> None:
    def lim(res, soft, hard=None):
        resource.setrlimit(res, (soft, hard or soft))

    lim(resource.RLIMIT_CPU,   CPU_SECONDS)
    lim(resource.RLIMIT_AS,    MEMORY_MB    * 1024 ** 2)
    lim(resource.RLIMIT_FSIZE, FILE_SIZE_MB * 1024 ** 2)
    lim(resource.RLIMIT_NOFILE, MAX_FDS)
    os.nice(10)


def run_sandboxed(job_id: str, script_path: str, work_dir: Path, publish: Callable[[str], None]) -> int:
    """
    Script'i sandbox içinde çalıştır, çıktıyı satır satır publish'e ilet.
    Döndürür: process return code (-1 = timeout, -2 = hata, -3 = kullanıcı durdurdu)
    """
    import redis as _redis
    _r = _redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

    try:
        process = subprocess.Popen(
            [_PYTHON, "-u", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(work_dir),
            env=SAFE_ENV,
            preexec_fn=_apply_limits,
            start_new_session=True,
        )
    except Exception as exc:
        publish(f"[Sandbox error: {exc}]")
        return -2

    # PID'i Redis'e yaz — stop endpoint buradan okur
    _r.hset(f"job:{job_id}", "pid", str(process.pid))

    try:
        for line in iter(process.stdout.readline, ""):
            # Her satırda stop isteği var mı kontrol et
            if _r.hget(f"job:{job_id}", "stop_requested") == "1":
                _kill_group(process.pid)
                process.wait()
                publish("[Stopped by user]")
                return -3
            publish(line.rstrip("\n"))
        process.stdout.close()
        process.wait(timeout=WALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_group(process.pid)
        process.wait()
        publish(f"[TIMEOUT: Job killed after {WALL_TIMEOUT}s]")
        return -1
    finally:
        _r.hdel(f"job:{job_id}", "pid", "stop_requested")

    return process.returncode


def _kill_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
