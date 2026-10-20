"""Recording and local playback shared by the profiles."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import subprocess
import time
import wave


def recording_path(record_dir: Path, profile: str, kind: str, suffix: str) -> Path:
    record_dir.mkdir(parents=True, exist_ok=True)
    return record_dir / f"{profile}-{time.strftime('%Y%m%d-%H%M%S')}-{kind.lower()}{suffix}"


def open_wav(path: Path, sample_rate: int) -> wave.Wave_write:
    recording = wave.open(str(path), "wb")
    recording.setnchannels(1)
    recording.setsampwidth(2)
    recording.setframerate(sample_rate)
    return recording


class Player:
    """Feeds audio to ffplay without ever blocking the Bluetooth event loop."""

    def __init__(self, input_args: list[str]) -> None:
        self.process: subprocess.Popen[bytes] | None = subprocess.Popen(
            ["ffplay", "-nodisp", "-loglevel", "error", "-probesize", "32", *input_args, "pipe:0"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert self.process.stdin is not None
        os.set_blocking(self.process.stdin.fileno(), False)

    def write(self, data: bytes) -> None:
        if not self.process or not self.process.stdin:
            return
        try:
            os.write(self.process.stdin.fileno(), data)
        except BlockingIOError:
            pass  # player is behind; drop this chunk rather than stall Bluetooth
        except OSError:
            self.close()  # ffplay exited; keep receiving

    def close(self) -> None:
        if self.process:
            with contextlib.suppress(OSError):
                self.process.stdin.close()
            self.process.terminate()
            self.process = None
