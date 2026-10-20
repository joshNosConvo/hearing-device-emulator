"""Bumble ASHA hearing-device emulator: the device side of Android's ASHA protocol.

Runs inside the Bumble virtual environment (Python 3.11+); the bumble backend
launches it for --mode asha. It presents an LE-only hearing device with the
configured side, pair membership and HiSyncId, reports what the phone asks for
(start/stop, audio type, volume) and can record or play the received audio.
Imports only the standard library and Bumble, so it never needs the CLI's modules.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time
import wave

from bumble import data_types, decoder, gatt
from bumble.core import AdvertisingData, Appearance
from bumble.device import AdvertisingType, Connection, Device, DeviceConfiguration
from bumble.profiles import asha
from bumble.profiles.gap import GenericAccessService
from bumble.transport import open_transport

SAMPLE_RATE = 16000
FRAME_SECONDS = 0.02  # each ASHA SDU: 1-byte sequence counter + 20 ms of 64 kbit/s G.722
ADVERTISING_INTERVAL_MS = 100  # hearing aids advertise fast; Bumble's default is 1 s
PROGRESS_SECONDS = 2.0
APPEARANCE = data_types.Appearance(Appearance.Category.HEARING_AID,
                                   Appearance.HearingAidSubcategory.COCHLEAR_IMPLANT)
FFPLAY = ["ffplay", "-nodisp", "-loglevel", "error", "-probesize", "32",
          "-f", "s16le", "-sample_rate", str(SAMPLE_RATE), "pipe:0"]


class SaturatingG722Decoder(decoder.G722Decoder):
    """Bumble's decoder omits the ITU reference's final 16-bit saturation and raises
    OverflowError on valid input (even an all-zero frame), which would end the stream."""

    def update_decoded_result(self, xout: int, byte_length: int, byte_array: bytearray) -> int:
        sample = max(-32768, min(32767, xout >> 11))
        byte_array[byte_length:byte_length + 2] = sample.to_bytes(2, "little", signed=True)
        return byte_length + 2


def signed_volume(value: int | None) -> int | None:
    """ASHA volume is an int8 in dB (-128 mute .. 0 max); Bumble keeps the raw byte."""
    return None if value is None else struct.unpack("b", bytes([value & 0xFF]))[0]


class Reporter:
    """Expose state to `status` (same file format as the microphone worker) and the log."""

    def __init__(self, status_file: Path | None) -> None:
        self.status_file = status_file

    def __call__(self, state: str, detail: str) -> None:
        if self.status_file:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            item = {"pid": os.getpid(), "state": state, "detail": detail, "updated": time.time()}
            draft = self.status_file.with_suffix(".tmp")
            draft.write_text(json.dumps(item), encoding="utf-8")
            draft.replace(self.status_file)
        print(f"[ASHA {time.strftime('%H:%M:%S')}] {state.upper()}: {detail}", flush=True)


class AudioStream:
    """One START..STOP stream: counts frames and sequence gaps, optionally records/plays."""

    def __init__(self, audio_type: str, record_dir: Path | None, play: bool) -> None:
        self.audio_type = audio_type
        self.frames = 0
        self.lost = 0
        self.last_sequence: int | None = None
        self.decoder = SaturatingG722Decoder() if record_dir or play else None
        self.wav: wave.Wave_write | None = None
        self.path: Path | None = None
        self.player: subprocess.Popen[bytes] | None = None
        if record_dir:
            record_dir.mkdir(parents=True, exist_ok=True)
            self.path = record_dir / f"asha-{time.strftime('%Y%m%d-%H%M%S')}-{audio_type.lower()}.wav"
            self.wav = wave.open(str(self.path), "wb")
            self.wav.setnchannels(1)
            self.wav.setsampwidth(2)
            self.wav.setframerate(SAMPLE_RATE)
        if play:
            self.player = subprocess.Popen(FFPLAY, stdin=subprocess.PIPE,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert self.player.stdin is not None
            os.set_blocking(self.player.stdin.fileno(), False)

    @property
    def seconds(self) -> float:
        return self.frames * FRAME_SECONDS

    def on_sdu(self, sdu: bytes) -> None:
        if not sdu:
            return
        sequence = sdu[0]
        if self.last_sequence is not None:
            self.lost += (sequence - self.last_sequence - 1) % 256
        self.last_sequence = sequence
        self.frames += 1
        if self.decoder is None:
            return
        pcm = self.decoder.decode_frame(sdu[1:])
        if self.wav:
            self.wav.writeframes(pcm)
        if self.player and self.player.stdin:
            try:
                os.write(self.player.stdin.fileno(), pcm)
            except BlockingIOError:
                pass  # player is behind; drop this frame rather than stall Bluetooth
            except OSError:
                self.player = None  # ffplay exited; keep receiving

    def close(self) -> None:
        if self.wav:
            self.wav.close()
        if self.player:
            with contextlib.suppress(OSError):
                self.player.stdin.close()
            self.player.terminate()
            self.player = None


class AshaEmulator:
    def __init__(self, device: Device, options: argparse.Namespace, report: Reporter) -> None:
        self.device = device
        self.options = options
        self.report = report
        self.stream: AudioStream | None = None
        # Bumble clears service.volume on STOP before emitting it; keep the last value for summaries.
        self.volume: int | None = None
        capability = asha.DeviceCapabilities(0)
        if options.side == "right":
            capability |= asha.DeviceCapabilities.IS_RIGHT
        if options.binaural:
            capability |= asha.DeviceCapabilities.IS_DUAL
        self.service = asha.AshaService(capability=capability, hisyncid=bytes.fromhex(options.sync_id),
                                        device=device, audio_sink=self._on_audio)
        device.add_service(GenericAccessService(options.name, APPEARANCE))
        device.add_service(self.service)
        self.service.on(self.service.EVENT_STARTED, self._on_started)
        self.service.on(self.service.EVENT_STOPPED, self._on_stopped)
        self.service.on(self.service.EVENT_VOLUME_CHANGED, self._on_volume)
        device.on(device.EVENT_CONNECTION, self._on_connection)

    def advertising_data(self) -> bytes:
        # Legacy advertising holds 31 bytes; the name goes in the scan response.
        return bytes(AdvertisingData([
            data_types.Flags(AdvertisingData.Flags.LE_GENERAL_DISCOVERABLE_MODE
                             | AdvertisingData.Flags.BR_EDR_NOT_SUPPORTED),
            data_types.IncompleteListOf16BitServiceUUIDs([gatt.GATT_ASHA_SERVICE]),
            APPEARANCE,
        ])) + self.service.get_advertising_data()

    def scan_response_data(self) -> bytes:
        return bytes(AdvertisingData([data_types.CompleteLocalName(self.options.name)]))

    async def start(self) -> None:
        await self.device.power_on()
        await self.device.start_advertising(
            advertising_type=AdvertisingType.UNDIRECTED_CONNECTABLE_SCANNABLE,
            auto_restart=True,
            advertising_data=self.advertising_data(),
            scan_response_data=self.scan_response_data(),
            advertising_interval_min=ADVERTISING_INTERVAL_MS,
            advertising_interval_max=ADVERTISING_INTERVAL_MS,
        )
        pair = f"{'binaural' if self.options.binaural else 'monaural'} {self.options.side}"
        self.report("advertising", f"'{self.options.name}' ({pair}, HiSyncId {self.options.sync_id}, "
                                   f"address {self.options.address}); pair from Android hearing device settings")

    async def report_progress(self) -> None:
        while True:
            await asyncio.sleep(PROGRESS_SECONDS)
            if self.stream:
                self.report("streaming", self._stream_summary())

    def close(self) -> None:
        self._end_stream()

    def _volume(self) -> str:
        if self.volume is None:
            return "unknown"
        return "muted" if self.volume == -128 else f"{self.volume} dB"

    def _stream_summary(self) -> str:
        assert self.stream is not None
        return (f"{self.stream.audio_type} audio, volume {self._volume()}, "
                f"{self.stream.seconds:.1f} s received, {self.stream.lost} frames lost")

    def _end_stream(self) -> str:
        if not self.stream:
            return ""
        summary = self._stream_summary()
        if self.stream.path:
            summary += f", recorded {self.stream.path}"
        self.stream.close()
        self.stream = None
        return summary

    def _on_connection(self, connection: Connection) -> None:
        peer = connection.peer_address
        self.report("connected", f"phone {peer} connected")
        connection.on(connection.EVENT_PAIRING,
                      lambda _keys: self.report("connected", f"bonded with {peer}"))
        connection.on(connection.EVENT_PAIRING_FAILURE,
                      lambda reason: self.report("connected", f"pairing with {peer} failed: {reason}"))
        connection.on(connection.EVENT_DISCONNECTION, lambda reason: self._on_disconnection(peer, reason))

    def _on_disconnection(self, peer: object, reason: int) -> None:
        summary = self._end_stream()
        detail = f"phone {peer} disconnected (reason 0x{reason:02X})"
        self.report("advertising", detail + (f"; stream ended: {summary}" if summary else ""))

    def _on_started(self) -> None:
        self._end_stream()
        audio_type = self.service.audio_type.name if self.service.audio_type is not None else "UNKNOWN"
        self.volume = signed_volume(self.service.volume)
        self.stream = AudioStream(audio_type, self.options.record_dir, self.options.play)
        other = "connected" if self.service.other_state else "not connected"
        self.report("streaming", f"START {audio_type} audio, volume {self._volume()}, other side {other}")

    def _on_stopped(self) -> None:
        summary = self._end_stream()
        self.report("connected", f"STOP; {summary}" if summary else "STOP")

    def _on_volume(self) -> None:
        self.volume = signed_volume(self.service.volume)
        self.report("streaming" if self.stream else "connected", f"volume set to {self._volume()}")

    def _on_audio(self, sdu: bytes) -> None:
        # An exception here would propagate into Bumble's L2CAP reassembly and break the channel.
        try:
            if self.stream:
                self.stream.on_sdu(sdu)
        except Exception as exc:  # noqa: BLE001 - keep the Bluetooth link alive
            logging.getLogger(__name__).warning("Dropped ASHA audio frame: %s", exc)


def device_config(options: argparse.Namespace) -> DeviceConfiguration:
    config = DeviceConfiguration()
    config.load_from_dict({
        "name": options.name,
        "address": options.address,
        "identity_address_type": 1,  # static random identity, like a hearing aid without a public address
        "keystore": f"JsonKeyStore:{options.keystore}" if options.keystore else None,
        "le_enabled": True,
        "classic_enabled": False,  # a real processor is LE-only: no classic profiles to pair instead
        "gap_service_enabled": False,  # replaced by one that carries the hearing-aid appearance
    })
    return config


async def run(options: argparse.Namespace) -> int:
    report = Reporter(options.status_file)
    async with await open_transport(options.transport) as hci:
        device = Device.from_config_with_hci(device_config(options), hci.source, hci.sink)
        emulator = AshaEmulator(device, options, report)
        await emulator.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        progress = asyncio.create_task(emulator.report_progress())
        stopped = asyncio.create_task(stop.wait())
        await asyncio.wait([stopped, hci.source.terminated], return_when=asyncio.FIRST_COMPLETED)
        progress.cancel()
        emulator.close()
        if not stop.is_set():
            report("stopped", "Bluetooth transport closed (adapter unplugged or claimed elsewhere?)")
            return 1
    report("stopped", "ASHA emulator stopped")
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bumble ASHA hearing-device emulator")
    parser.add_argument("--transport", required=True, help="Bumble transport, e.g. usb:0")
    parser.add_argument("--name", required=True)
    parser.add_argument("--side", choices=["left", "right"], default="left")
    parser.add_argument("--binaural", action="store_true", help="One side of a two-sided set")
    parser.add_argument("--sync-id", required=True, help="HiSyncId, 16 hex digits")
    parser.add_argument("--address", required=True, help="Static random address, XX:XX:XX:XX:XX:XX")
    parser.add_argument("--keystore", type=Path, help="JSON file that keeps bonds across restarts")
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--record-dir", type=Path, help="Write each received stream as a 16 kHz WAV")
    parser.add_argument("--play", action="store_true", help="Play received audio through ffplay")
    return parser


def main() -> int:
    options = make_parser().parse_args()
    if len(bytes.fromhex(options.sync_id)) != 8:
        print("[ASHA ERROR] --sync-id must be 16 hex digits", file=sys.stderr)
        return 2
    logging.basicConfig(level=os.environ.get("BUMBLE_LOGLEVEL", "WARNING").upper())
    try:
        return asyncio.run(run(options))
    except Exception as exc:  # noqa: BLE001 - surface transport/controller errors in the log
        Reporter(options.status_file)("stopped", f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
