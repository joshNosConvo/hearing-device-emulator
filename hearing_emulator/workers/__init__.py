"""Long-running processes that backends launch, separate from the CLI process.

Each worker runs with `python -m hearing_emulator.workers.<name>` and reports its
state in ~/.hearing-emulator/<label>-status.json for `status`.
"""
