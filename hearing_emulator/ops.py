"""Shell execution with explicit commands, failures, and safe defaults."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

DATA = Path.home() / ".hearing-emulator"


def info(message: str) -> None:
    print(f"[INFO] {message}", flush=True)


def ok(message: str) -> None:
    print(f"[ OK ] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[WARN] {message}", flush=True)


def error(message: str) -> None:
    print(f"[FAIL] {message}", file=sys.stderr, flush=True)


def has(binary: str) -> bool:
    return shutil.which(binary) is not None


def capture(cmd: list[str], *, timeout: int = 12) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def execute(cmd: list[str], *, cwd: Path | None = None) -> None:
    info("Running: " + " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def is_ubuntu() -> bool:
    os_release = Path("/etc/os-release")
    return sys.platform.startswith("linux") and os_release.exists() and (
        'ID=ubuntu' in os_release.read_text() or 'ID="ubuntu"' in os_release.read_text()
    )


def ensure_unprivileged() -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ValueError("Run this tool as your regular user; installers invoke sudo when needed")
