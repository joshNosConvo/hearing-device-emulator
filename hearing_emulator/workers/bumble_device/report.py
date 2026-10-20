"""Per-profile state for `status` and the log."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time


class Reporter:
    """Writes the worker's status file and logs every change.

    The file keeps the microphone worker's format (pid, state, detail) for the latest
    change, plus `profiles`: the current state and detail of each profile.
    """

    def __init__(self, status_file: Path | None) -> None:
        self.status_file = status_file
        self.profiles: dict[str, dict[str, str]] = {}

    def __call__(self, profile: str, state: str, detail: str) -> None:
        self.profiles[profile] = {"state": state, "detail": detail}
        if self.status_file:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            item = {"pid": os.getpid(), "state": state, "detail": f"{profile}: {detail}",
                    "profiles": self.profiles, "updated": time.time()}
            draft = self.status_file.with_suffix(".tmp")
            draft.write_text(json.dumps(item), encoding="utf-8")
            draft.replace(self.status_file)
        print(f"[{profile.upper()} {time.strftime('%H:%M:%S')}] {state.upper()}: {detail}", flush=True)
