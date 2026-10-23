"""Composes the configured profiles on one Bumble device and serves the control socket."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import signal
import sys
from collections.abc import Awaitable, Callable

from bumble import avdtp, hci, rfcomm
from bumble import hfp as bumble_hfp
from bumble.core import PhysicalTransport
from bumble.device import Connection, Device, DeviceConfiguration
from bumble.transport import open_transport

from ..microphone import pcm_silence, pcm_tone, pcm_wave, validate_wave
from .a2dp import A2dpSink
from .asha import AshaEmulator
from .hfp import SAMPLE_RATE as HFP_SAMPLE_RATE, HandsFree, PcmSource
from .l2cap_compat import accept_flush_timeout
from .report import Reporter

PROFILES = ("asha-sink", "a2dp-sink", "hfp-hf")
PROGRESS_SECONDS = 2.0
CONNECT_TIMEOUT = 20.0
# Class of Device: major class Audio/Video, service classes Audio and Rendering.
HEADSET_CLASS = 0x240404  # minor class: wearable headset (offered with HFP)
SPEAKER_CLASS = 0x240414  # minor class: loudspeaker (A2DP only)


def device_config(options: argparse.Namespace) -> DeviceConfiguration:
    profiles = set(options.profiles)
    config = DeviceConfiguration()
    config.load_from_dict({
        "name": options.name,
        "address": options.address,  # static random LE identity
        "identity_address_type": 1,
        "keystore": f"JsonKeyStore:{options.keystore}" if options.keystore else None,
        # A real processor is LE-only; Classic is on only for the A2DP/HFP profiles.
        "le_enabled": "asha-sink" in profiles,
        "classic_enabled": bool(profiles & {"a2dp-sink", "hfp-hf"}),
        "class_of_device": HEADSET_CLASS if "hfp-hf" in profiles else SPEAKER_CLASS,
        # ASHA replaces the GAP service with one that carries the hearing-aid appearance.
        "gap_service_enabled": "asha-sink" not in profiles,
    })
    return config


def microphone_source(options: argparse.Namespace) -> PcmSource | None:
    if "hfp-hf" not in options.profiles or not options.mic_source:
        return None
    if options.mic_source == "silence":
        return PcmSource(pcm_silence(sample_rate=HFP_SAMPLE_RATE), "silence")
    if options.mic_source == "wav":
        return PcmSource(pcm_wave(Path(options.mic_wave_path), HFP_SAMPLE_RATE), Path(options.mic_wave_path).name)
    return PcmSource(pcm_tone(sample_rate=HFP_SAMPLE_RATE, frequency=options.mic_frequency, gain=options.mic_gain),
                     f"{options.mic_frequency:g} Hz tone")


class DeviceEmulator:
    def __init__(self, device: Device, options: argparse.Namespace, report: Reporter) -> None:
        self.device = device
        self.options = options
        self.report = report
        profiles = set(options.profiles)
        self.asha = AshaEmulator(device, options, report) if "asha-sink" in profiles else None
        self.a2dp = A2dpSink(device, report, options.record_dir, options.play,
                             offer_aac=options.codec == "aac") if "a2dp-sink" in profiles else None
        self.hfp = HandsFree(device, report, microphone_source(options),
                             options.record_dir) if "hfp-hf" in profiles else None
        self.classic = bool(self.a2dp or self.hfp)
        if self.classic:
            accept_flush_timeout()  # an iPhone never streams A2DP without it
        device.on(device.EVENT_CONNECTION, self._on_connection)

    @property
    def profiles(self) -> list:
        return [profile for profile in (self.asha, self.a2dp, self.hfp) if profile]

    async def start(self) -> None:
        await self.device.power_on()
        if self.hfp:
            await self.hfp.start()
        if self.asha:
            await self.asha.start_advertising()
        if self.classic:
            await self.device.set_discoverable(True)
            await self.device.set_connectable(True)
            names = ", ".join(name for name, profile in (("A2DP", self.a2dp), ("HFP", self.hfp)) if profile)
            self.report("classic", "discoverable",
                        f"'{self.options.name}' ({names}) at {self.device.public_address.to_string(False)}; "
                        "pair from the phone's Bluetooth settings")

    async def report_progress(self) -> None:
        while True:
            await asyncio.sleep(PROGRESS_SECONDS)
            for profile in self.profiles:
                profile.progress()

    def close(self) -> None:
        for profile in self.profiles:
            profile.close()

    async def control(self, request: dict) -> dict:
        """Commands from the CLI: status, disconnect <address>, connect <address>."""
        command = request.get("command")
        if command == "status":
            return {"ok": True, "connections": self._connections(), "profiles": self.report.profiles}
        address = str(request.get("address", "")).upper()
        if command == "disconnect":
            matches = [connection for connection in list(self.device.connections.values())
                       if address in self._addresses(connection)]
            if not matches:
                return {"ok": False, "detail": f"no connection to {address}; "
                                               f"connected: {', '.join(self._connections()) or 'none'}"}
            for connection in matches:
                await connection.disconnect()
            links = ", ".join(self._transport(connection) for connection in matches)
            return {"ok": True, "detail": f"disconnected {address} ({links}); the emulator stays available, "
                                          "so the phone decides whether to reconnect"}
        if command == "connect":
            return await self._connect(address)
        return {"ok": False, "detail": f"unknown command: {command}"}

    async def _connect(self, address: str) -> dict:
        notes = []
        if self.asha:
            notes.append("ASHA reconnects are started by the phone; the emulator keeps advertising for it")
        if not self.classic:
            return {"ok": True, "detail": "; ".join(notes)}
        try:
            connection = await self.device.connect(hci.Address(address, hci.Address.PUBLIC_DEVICE_ADDRESS),
                                                   transport=PhysicalTransport.BR_EDR, timeout=CONNECT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - report to the CLI
            return {"ok": False, "detail": "; ".join([f"Classic connection to {address} failed: {exc}", *notes])}
        opened = ["Classic link"]
        try:
            await connection.authenticate()
            await connection.encrypt()
        except Exception as exc:  # noqa: BLE001 - profiles may still open; report it
            notes.append(f"encryption not set up ({exc})")
        if self.a2dp:
            try:
                server = await avdtp.Protocol.connect(connection)
                self.a2dp.listener.set_server(connection, server)
                self.a2dp.attach(server)
                opened.append("A2DP")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"A2DP failed: {exc}")
        if self.hfp:
            try:
                record = await bumble_hfp.find_ag_sdp_record(connection)
                if record is None:
                    notes.append("HFP: the phone offers no audio gateway")
                else:
                    multiplexer = await rfcomm.Client(connection).start()
                    self.hfp.attach(await multiplexer.open_dlc(record[0]))
                    opened.append("HFP")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"HFP failed: {exc}")
        detail = f"connected to {address}: {', '.join(opened)}"
        return {"ok": True, "detail": "; ".join([detail, *notes])}

    @staticmethod
    def _transport(connection: Connection) -> str:
        return "LE" if connection.transport == PhysicalTransport.LE else "Classic"

    @staticmethod
    def _addresses(connection: Connection) -> set[str]:
        addresses = {connection.peer_address.to_string(False)}
        if resolvable := getattr(connection, "peer_resolvable_address", None):
            addresses.add(resolvable.to_string(False))  # Android connects with a private address
        return addresses

    def _connections(self) -> list[str]:
        return [f"{connection.peer_address.to_string(False)} ({self._transport(connection)})"
                for connection in self.device.connections.values()]

    def _on_connection(self, connection: Connection) -> None:
        if connection.transport != PhysicalTransport.BR_EDR:
            return  # LE links belong to ASHA
        peer = connection.peer_address.to_string(False)
        self.report("classic", "connected", f"phone {peer} connected")
        connection.on(connection.EVENT_PAIRING,
                      lambda _keys: self.report("classic", "connected", f"bonded with {peer}"))
        connection.on(connection.EVENT_PAIRING_FAILURE,
                      lambda reason: self.report("classic", "connected", f"pairing with {peer} failed: {reason}"))
        connection.on(connection.EVENT_DISCONNECTION,
                      lambda reason: self.report("classic", "discoverable",
                                                 f"phone {peer} disconnected (reason 0x{reason:02X})"))


async def serve_control(path: Path, handler: Callable[[dict], Awaitable[dict]]) -> asyncio.AbstractServer:
    """One JSON request line in, one JSON response line out, on a user-only Unix socket."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            response = await handler(json.loads(await reader.readline()))
        except Exception as exc:  # noqa: BLE001 - answer the CLI instead of dropping it
            response = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        writer.write(json.dumps(response).encode() + b"\n")
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(on_client, path=str(path))
    path.chmod(0o600)
    return server


async def run(options: argparse.Namespace) -> int:
    report = Reporter(options.status_file)
    async with await open_transport(options.transport) as hci_transport:
        device = Device.from_config_with_hci(device_config(options), hci_transport.source, hci_transport.sink)
        emulator = DeviceEmulator(device, options, report)
        await emulator.start()
        control = None
        if options.control_socket:
            try:
                control = await serve_control(options.control_socket, emulator.control)
            except OSError as exc:  # the emulator still works; only CLI connect/disconnect is lost
                report("device", "running", f"control socket unavailable ({exc}); scenario connect/disconnect "
                                            "and live connections won't work")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        progress = asyncio.create_task(emulator.report_progress())
        stopped = asyncio.create_task(stop.wait())
        await asyncio.wait([stopped, hci_transport.source.terminated], return_when=asyncio.FIRST_COMPLETED)
        progress.cancel()
        emulator.close()
        if control:
            control.close()
            options.control_socket.unlink(missing_ok=True)
        if not stop.is_set():
            report("device", "stopped", "Bluetooth transport closed (adapter unplugged or claimed elsewhere?)")
            return 1
    report("device", "stopped", "emulator stopped")
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bumble hearing-device emulator")
    parser.add_argument("--transport", required=True, help="Bumble transport, e.g. usb:0 or usb:0+sco=0")
    parser.add_argument("--name", required=True)
    parser.add_argument("--profiles", required=True, help=f"Comma-separated: {', '.join(PROFILES)}")
    parser.add_argument("--address", required=True, help="Static random LE address, XX:XX:XX:XX:XX:XX")
    parser.add_argument("--side", choices=["left", "right"], default="left", help="ASHA side")
    parser.add_argument("--binaural", action="store_true", help="ASHA: one side of a two-sided set")
    parser.add_argument("--sync-id", help="ASHA HiSyncId, 16 hex digits")
    parser.add_argument("--codec", choices=["sbc", "aac"], default="sbc", help="A2DP: aac also offers AAC")
    parser.add_argument("--mic-source", choices=["tone", "silence", "wav"], default="tone")
    parser.add_argument("--mic-frequency", type=float, default=440.0)
    parser.add_argument("--mic-gain", type=float, default=0.2)
    parser.add_argument("--mic-wave-path", type=Path, help="8 kHz mono 16-bit WAV for --mic-source wav")
    parser.add_argument("--keystore", type=Path, help="JSON file that keeps bonds across restarts")
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--control-socket", type=Path, help="Unix socket for connect/disconnect/status")
    parser.add_argument("--record-dir", type=Path, help="Save each received stream")
    parser.add_argument("--play", action="store_true", help="Play received audio through ffplay")
    return parser


def main() -> int:
    options = make_parser().parse_args()
    options.profiles = [profile for profile in options.profiles.split(",") if profile]
    try:
        unknown = set(options.profiles) - set(PROFILES)
        if unknown or not options.profiles:
            raise ValueError(f"--profiles must name some of: {', '.join(PROFILES)}")
        if "asha-sink" in options.profiles and len(bytes.fromhex(options.sync_id or "")) != 8:
            raise ValueError("--sync-id must be 16 hex digits for asha-sink")
        if "hfp-hf" in options.profiles and options.mic_source == "wav":
            validate_wave(Path(options.mic_wave_path or ""), HFP_SAMPLE_RATE)
    except ValueError as exc:
        print(f"[DEVICE ERROR] {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=os.environ.get("BUMBLE_LOGLEVEL", "WARNING").upper())
    try:
        return asyncio.run(run(options))
    except Exception as exc:  # noqa: BLE001 - surface transport/controller errors in the log
        Reporter(options.status_file)("device", "stopped", f"{type(exc).__name__}: {exc}")
        return 1
