import json
import os
import subprocess
import sys
import time
from threading import Thread

import config

LOCK_FILE = "/tmp/bibliorecs_update.lock"
STATUS_FILE = os.path.join(os.path.dirname(__file__), "update_status.json")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def start():
    """Start the daemon background updater thread."""
    t = Thread(target=_loop, daemon=True)
    t.start()


def read_status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ──────────────────────────── loop ────────────────────────────


def _loop():
    while True:
        hour = time.localtime().tm_hour

        if config.UPDATE_WINDOW_START <= hour < config.UPDATE_WINDOW_END:
            _run_due_tasks()
            time.sleep(120)
        else:
            time.sleep(300)


def _run_due_tasks():
    s = read_status() or {}
    today = time.strftime("%Y-%m-%d")
    daily_last_date = s.get("daily", {}).get("last_run_date", "")

    if daily_last_date != today:
        _run("daily.py", "daily")


# ──────────────────────────── task runner ────────────────────────────


def _run(script, task_name):
    if not _acquire_lock():
        return

    _set_status("now", f"running {script}")
    ts_start = time.strftime("%Y-%m-%dT%H:%M:%S")
    t0 = time.monotonic()

    remaining = (config.UPDATE_WINDOW_END - time.localtime().tm_hour) * 3600 - 60
    timeout = max(min(remaining, 7200), 600)

    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True,
            cwd=_SCRIPT_DIR,
            timeout=timeout,
        )

        elapsed = round(time.monotonic() - t0)
        ok = result.returncode == 0
        info = {"last_run": ts_start, "last_run_date": time.strftime("%Y-%m-%d"),
                "duration_sec": elapsed, "state": "ok" if ok else "failed"}
        if ok:
            info["last_ok"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            stderr = result.stderr.strip()
            info["error"] = stderr[-500:] if stderr else f"exit {result.returncode}"
            info["last_ok"] = _read_nested(status(), f"{task_name}.last_ok")

        _set_status(task_name, info)

    except subprocess.TimeoutExpired:
        _set_status(task_name, {"last_run": ts_start, "last_run_date": time.strftime("%Y-%m-%d"),
                                "duration_sec": timeout, "state": "failed", "error": "timed out"})
    except Exception as e:
        elapsed = round(time.monotonic() - t0)
        _set_status(task_name, {"last_run": ts_start, "last_run_date": time.strftime("%Y-%m-%d"),
                                "duration_sec": elapsed, "state": "failed", "error": str(e)[:500]})
    finally:
        _set_status("now", "idle")
        _release_lock()


# ──────────────────────────── file helpers ────────────────────────────


def _acquire_lock():
    if os.path.exists(LOCK_FILE):
        age = time.time() - os.path.getmtime(LOCK_FILE)
        if age < 21600:
            return False
        os.remove(LOCK_FILE)
    with open(LOCK_FILE, "w") as f:
        f.write(str(time.time()))
    return True


def _release_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


def _set_status(key, value):
    s = status()
    s[key] = value
    _write(s)


def status():
    return read_status() or {"now": "idle"}


def _write(s):
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


def _read_nested(s, path):
    if not s:
        return None
    for p in path.split("."):
        if isinstance(s, dict):
            s = s.get(p)
        else:
            return None
    return s


def _parse_time(t_str):
    if not t_str:
        return 0
    try:
        return time.mktime(time.strptime(t_str, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OSError):
        return 0
