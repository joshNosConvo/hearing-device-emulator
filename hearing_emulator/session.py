"""Managed local background processes, logs, and process identity checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .ops import DATA, info, warn

STATE = DATA / "session.json"
LOGS = DATA / "logs"
_OWNED: dict[int, subprocess.Popen] = {}


def _read() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _start_time(pid: int) -> str:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                            text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _matches(entry: dict) -> bool:
    """Refuse to manage a PID unless it is still the process we launched.

    The launch time identifies it across PID reuse. Matching argv[0] against `ps`
    is only a fallback for older session files: interpreters that re-execute
    themselves (macOS framework Python) report a different command line.
    """
    pid = entry["pid"]
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    if entry.get("start_time"):
        return _start_time(pid) == entry["start_time"]
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                            text=True, capture_output=True, check=False)
    return result.returncode == 0 and entry["executable"] in result.stdout


def _exited(entry: dict) -> bool:
    child = _OWNED.get(entry["pid"])
    if child is not None:
        # Our own child lingers as a zombie until reaped; poll() reaps it.
        return child.poll() is not None
    return not _matches(entry)


def status() -> dict:
    state = _read()
    if not state:
        return {}
    state["processes"] = [{**entry, "alive": _matches(entry)}
                          for entry in state.get("processes", [])]
    return state


def start(backend: str, commands: list[tuple[str, list[str]]]) -> dict:
    previous = status()
    if any(p["alive"] for p in previous.get("processes", [])):
        raise RuntimeError("An emulator process is already running. Use 'stop' first")
    LOGS.mkdir(parents=True, exist_ok=True)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    try:
        for label, argv in commands:
            logfile = LOGS / f"{backend}-{label}.log"
            info(f"Starting {label}: {' '.join(argv)}")
            with logfile.open("a", encoding="utf-8") as stream:
                stream.write(f"\n\n--- Launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                stream.flush()
                proc = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, start_new_session=True)
            _OWNED[proc.pid] = proc
            entries.append({"label": label, "pid": proc.pid, "start_time": _start_time(proc.pid),
                            "executable": argv[0], "argv": argv, "log": str(logfile)})
            time.sleep(1.0 if label in {"daemon", "speaker"} else 0.5)
            if proc.poll() is not None:
                excerpt = logfile.read_text(encoding="utf-8", errors="replace")[-2400:]
                raise RuntimeError(f"{label} exited with code {proc.returncode}. Log:\n{excerpt}")
        state = {"backend": backend, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "processes": entries}
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return state
    except Exception:
        for entry in entries:
            _stop_entry(entry)
        raise


def _stop_entry(entry: dict) -> None:
    pid = entry["pid"]
    if not _matches(entry):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        warn(f"No permission to stop PID {pid} ({entry['label']}); it may run as another user")
        return
    for _ in range(15):
        if _exited(entry):
            _OWNED.pop(pid, None)
            return
        time.sleep(0.1)
    # Never SIGKILL unknown/reused PID; caller can inspect if stubborn.
    warn(f"PID {pid} still appears active after SIGTERM")


def stop() -> None:
    state = status()
    if not state:
        info("No managed emulator session found")
        return
    for entry in reversed(state.get("processes", [])):
        _stop_entry(entry)
    STATE.unlink(missing_ok=True)


def log_files() -> list[Path]:
    if not LOGS.exists():
        return []
    return sorted(LOGS.glob("*.log"))
