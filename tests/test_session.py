import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from hearing_emulator import session


class SessionTests(unittest.TestCase):
    def test_start_status_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "session.json"
            logs = Path(directory) / "logs"
            with patch.object(session, "STATE", state), patch.object(session, "LOGS", logs):
                try:
                    session.start("dummy", [("test", [sys.executable, "-c", "import time;time.sleep(30)"])])
                    current = session.status()
                    self.assertEqual(current["backend"], "dummy")
                    self.assertTrue(current["processes"][0]["alive"])
                    self.assertTrue((logs / "dummy-test.log").exists())
                finally:
                    session.stop()
                self.assertFalse(state.exists())

    def test_reused_pid_is_not_ours(self):
        # A live PID with a different launch time must never be signalled.
        me = {"label": "x", "pid": os.getpid(), "executable": sys.executable,
              "start_time": "Thu Jan  1 00:00:00 1970"}
        self.assertFalse(session._matches(me))
        me["start_time"] = session._start_time(os.getpid())
        self.assertTrue(session._matches(me))


if __name__ == "__main__":
    unittest.main()
