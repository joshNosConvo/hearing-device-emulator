"""Optional Linux BlueALSA HFP microphone stream.

On a BlueALSA HFP-HF (hands-free) node, ALSA SCO *playback* represents audio
sent from the emulated headset microphone toward the phone (HFP AG). This is
independent of BlueALSA's `bluealsa-aplay` which handles incoming phone audio.

This worker runs until stopped, waiting for a paired HFP SCO playback endpoint,
then pipes 8/16 kHz mono PCM from a selectable source to `aplay`. Not a proof
that the phone selected HFP or successfully captured audio.
"""
from __future__ import annotations

import argparse
import contextlib
import math
import os
from pathlib import Path
import re
import select
import signal
import struct
import subprocess
import sys
import time
import json
import wave
from typing import Iterator

from ..config import MIC_SOURCES
from ..ops import capture, DATA

CHUNK_MS = 20
STOP = False
STATUS_FILE = DATA / "microphone-status.json"


def set_status(state: str, detail: str) -> None:
    """Expose worker state separately from process RUNNING status."""
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    item = {"pid": os.getpid(), "state": state, "detail": detail, "updated": time.time()}
    target = STATUS_FILE.with_suffix(".tmp")
    target.write_text(json.dumps(item), encoding="utf-8")
    target.replace(STATUS_FILE)
    emit(f"{state.upper()}: {detail}")


def current_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
MAC_RE = re.compile(r"^(?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}$")


def emit(message: str) -> None:
    print(f"[MIC {time.strftime('%H:%M:%S')}] {message}", flush=True)


def on_shutdown(signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def pcm_device(peer: str | None) -> str:
    if peer and not MAC_RE.fullmatch(peer):
        raise ValueError("--peer must be a Bluetooth MAC address")
    # HWCOMPAT=busy makes the PCM fail to open until SCO is acquired by the phone.
    # Without it the PCM may open and then block indefinitely waiting for audio.
    dev = peer or "00:00:00:00:00:00"
    return f"bluealsa:DEV={dev},PROFILE=sco,HWCOMPAT=busy"


def has_sco_playback(listing: str, peer: str | None = None) -> bool:
    """Check BlueALSA's PCM list for SCO playback, not merely a paired device."""
    target = (peer or "").lower()
    lines = listing.lower().splitlines()
    for index, line in enumerate(lines):
        if "profile=sco" not in line:
            continue
        if target and target not in line:
            continue
        # bluealsa-aplay -L emits PCM name and its description on nearby lines.
        description = " ".join(lines[index:index + 3])
        if "playback" in description:
            return True
    return False


def sco_available(peer: str | None = None) -> bool:
    response = capture(["bluealsa-aplay", "-L"], timeout=6)
    return response.returncode == 0 and has_sco_playback(response.stdout, peer)


def pcm_tone(*, sample_rate: int, frequency: float, gain: float,
             chunk_ms: int = CHUNK_MS) -> Iterator[bytes]:
    frames = sample_rate * chunk_ms // 1000
    position = 0
    while True:
        chunk = bytearray()
        for i in range(frames):
            sample = int(32767 * gain * math.sin(2 * math.pi * frequency * (position + i) / sample_rate))
            chunk += struct.pack("<h", sample)
        position += frames
        yield bytes(chunk)


def pcm_silence(*, sample_rate: int, chunk_ms: int = CHUNK_MS) -> Iterator[bytes]:
    chunk = b"\0\0" * (sample_rate * chunk_ms // 1000)
    while True:
        yield chunk


def validate_wave(path: Path, sample_rate: int) -> None:
    if not path.is_file():
        raise ValueError(f"Microphone WAV file does not exist: {path}")
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                or audio.getframerate() != sample_rate or audio.getnframes() == 0
                or audio.getcomptype() != "NONE"):
            raise ValueError(f"Expected nonempty, uncompressed 16-bit mono {sample_rate} Hz WAV: {path}")


def pcm_wave(path: Path, sample_rate: int, chunk_ms: int = CHUNK_MS) -> Iterator[bytes]:
    validate_wave(path, sample_rate)
    frames = sample_rate * chunk_ms // 1000
    with wave.open(str(path), "rb") as audio:
        while True:
            data = audio.readframes(frames)
            if not data:
                audio.rewind()
                continue
            yield data


def generate_sample(path: Path, *, seconds: float, sample_rate: int,
                    frequency: float, gain: float) -> None:
    if not 0 < seconds <= 120:
        raise ValueError("Sample duration must be greater than 0 and at most 120 seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = round(sample_rate * seconds)
    samples = bytearray()
    for i in range(frames):
        value = int(32767 * gain * math.sin(2 * math.pi * frequency * i / sample_rate))
        samples += struct.pack("<h", value)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples)


def _stream_file_or_tone(args: argparse.Namespace, aplay: subprocess.Popen[bytes]) -> None:
    if args.source == "tone":
        chunks = pcm_tone(sample_rate=args.sample_rate, frequency=args.frequency, gain=args.gain)
    elif args.source == "silence":
        chunks = pcm_silence(sample_rate=args.sample_rate)
    else:
        chunks = pcm_wave(Path(args.wave_path), args.sample_rate)
    _feed(chunks, aplay, peer=args.peer)


def _feed(chunks: Iterator[bytes], aplay: subprocess.Popen[bytes],
          input_process: subprocess.Popen[bytes] | None = None, peer: str | None = None) -> None:
    """Write bounded chunks, stop on disconnection, never busy-wait a full pipe.

    The SCO endpoint is probed only while writes are stalled. Probing spawns
    bluealsa-aplay, which takes longer than aplay's 60 ms buffer, so probing while
    audio flows would underrun it. A released transport needs no probe:
    HWCOMPAT=busy puts the PCM in the disconnected state and aplay exits.
    """
    assert aplay.stdin is not None
    fd = aplay.stdin.fileno()
    os.set_blocking(fd, False)
    last_probe = time.monotonic()
    acknowledged = False
    for chunk in chunks:
        if STOP or aplay.poll() is not None:
            return
        view = memoryview(chunk)
        while view and not STOP and aplay.poll() is None:
            _, writable, _ = select.select([], [fd], [], 0.4)
            if input_process and input_process.poll() is not None:
                emit("Microphone ALSA input stopped")
                return
            if not writable:
                if time.monotonic() - last_probe >= 2:
                    if not sco_available(peer):
                        set_status("waiting", "SCO endpoint lost during playback")
                        return
                    last_probe = time.monotonic()
                continue
            try:
                count = os.write(fd, view)
            except (BrokenPipeError, OSError) as exc:
                emit(f"SCO playback write ended: {exc}")
                return
            view = view[count:]
            if count and not acknowledged:
                set_status("feeding_pcm", "PCM accepted by ALSA process; phone capture UNVERIFIED")
                acknowledged = True


def _stream_alsa(args: argparse.Namespace, aplay: subprocess.Popen[bytes]) -> None:
    command = ["arecord", "-D", args.alsa_device, "-t", "raw", "-f", "S16_LE",
               "-c", "1", "-r", str(args.sample_rate),
               "--period-time=10000", "--buffer-time=30000"]
    emit(f"Capturing microphone from ALSA device {args.alsa_device}")
    capture_process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert capture_process.stdout is not None
        chunk_bytes = args.sample_rate * CHUNK_MS // 1000 * 2

        def chunks() -> Iterator[bytes]:
            while not STOP and capture_process.poll() is None:
                data = capture_process.stdout.read(chunk_bytes)
                if not data:
                    break
                yield data

        _feed(chunks(), aplay, capture_process, peer=args.peer)
    finally:
        capture_process.terminate() if capture_process.poll() is None else None
        with contextlib.suppress(subprocess.TimeoutExpired):
            capture_process.wait(timeout=2)
        if capture_process.poll() is None:
            capture_process.kill()
            capture_process.wait()
        capture_process.stdout.close()


def run_worker(args: argparse.Namespace) -> None:
    signal.signal(signal.SIGTERM, on_shutdown)
    signal.signal(signal.SIGINT, on_shutdown)
    device = pcm_device(args.peer)
    if args.source == "wav":
        validate_wave(Path(args.wave_path), args.sample_rate)
    emit(f"Worker started, source={args.source}, rate={args.sample_rate}Hz, target=HFP SCO")
    set_status("waiting", "No HFP SCO playback PCM; phone must select HFP audio route")
    was_available = False
    while not STOP:
        available = sco_available(args.peer)
        if not available:
            if was_available:
                set_status("waiting", "HFP SCO playback endpoint unavailable")
            was_available = False
            time.sleep(args.retry_seconds)
            continue
        was_available = True
        # Using -D BlueALSA PCM for HFP-HF *playback* is microphone audio toward phone.
        command = ["aplay", "-q", "-D", device, "-t", "raw", "-f", "S16_LE",
                   "-r", str(args.sample_rate), "-c", "1",
                   "--period-time=20000", "--buffer-time=60000"]
        set_status("opening_sco", "HFP PCM detected; opening ALSA SCO playback")
        playback = subprocess.Popen(command, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            if args.source == "alsa":
                _stream_alsa(args, playback)
            else:
                _stream_file_or_tone(args, playback)
        finally:
            if playback.poll() is None:
                playback.terminate()
            try:
                _, stderr = playback.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                playback.kill()
                _, stderr = playback.communicate()
            if stderr and not STOP:
                emit("aplay: " + stderr.decode("utf-8", errors="replace").strip()[-600:])
        if not STOP:
            set_status("waiting", "Microphone stream ended; retrying HFP SCO")
            time.sleep(args.retry_seconds)
    set_status("stopped", "Microphone worker stopped")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BlueALSA HFP microphone PCM worker")
    parser.add_argument("--source", choices=MIC_SOURCES, default="tone")
    parser.add_argument("--wave-path")
    parser.add_argument("--alsa-device", default="default")
    parser.add_argument("--peer")
    parser.add_argument("--sample-rate", type=int, choices=[8000, 16000], default=8000)
    parser.add_argument("--frequency", type=float, default=440)
    parser.add_argument("--gain", type=float, default=0.2)
    parser.add_argument("--retry-seconds", type=float, default=2)
    parser.add_argument("--generate-sample", type=Path)
    parser.add_argument("--seconds", type=float, default=3)
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        if not 20 <= args.frequency <= 3500:
            raise ValueError("Tone frequency must be within 20..3500 Hz")
        if not 0 <= args.gain <= 1:
            raise ValueError("Tone gain must be between 0 and 1")
        if args.retry_seconds < 0.5:
            raise ValueError("Retry interval must be at least 0.5 seconds")
        if args.source == "wav" and not args.wave_path and not args.generate_sample:
            raise ValueError("--wave-path is required for WAV microphone source")
        if args.generate_sample:
            generate_sample(args.generate_sample, seconds=args.seconds,
                            sample_rate=args.sample_rate, frequency=args.frequency,
                            gain=args.gain)
            emit(f"Created offline microphone test file: {args.generate_sample}")
        else:
            run_worker(args)
        return 0
    except (ValueError, OSError, wave.Error, subprocess.SubprocessError) as exc:
        print(f"[MIC ERROR] {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
