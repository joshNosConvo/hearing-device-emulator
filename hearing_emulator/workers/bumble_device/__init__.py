"""Emulated Bluetooth hearing device on Bumble (runs in the Bumble venv, Python 3.11+).

One process and one adapter present every configured profile at once: ASHA over LE,
A2DP and HFP over Classic. Launch with `python -m hearing_emulator.workers.bumble_device`.
"""
