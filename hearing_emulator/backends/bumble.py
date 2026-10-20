"""Bumble backend: one emulated device (ASHA, A2DP, HFP) on a USB adapter, macOS or Linux."""
from __future__ import annotations

import hashlib
from pathlib import Path
import json
import os
import socket
import sys
import tempfile

from ..config import Config
from ..ops import DATA, capture, ensure_unprivileged, execute, has, ok, is_ubuntu, warn

VENV = DATA / "venv-bumble"
# Realtek dongles run on firmware that Bumble's rtk driver uploads at power-on. All images
# are fetched (~0.7 MB) so any Realtek dongle Bumble knows works. bumble-rtk-fw-download
# itself fails in Bumble 0.0.236: it imports 'tools.rtk_util', which only resolves from a
# source checkout, so run its downloader with that module aliased.
REALTEK_DOWNLOAD = ("import sys, bumble.tools, bumble.tools.rtk_util; "
                    "sys.modules.setdefault('tools', bumble.tools); "
                    "from bumble.tools.rtk_fw_download import main; main()")
# Prints the Realtek USB IDs Bumble drives, then how many firmware images are downloaded.
REALTEK_STATUS = ("from bumble.drivers.rtk import RTK_USB_PRODUCTS, Driver, rtk_firmware_dir; "
                  "print(' '.join(f'{v:04X}:{p:04X}' for v, p in sorted(RTK_USB_PRODUCTS))); "
                  "print(sum((rtk_firmware_dir() / d.fw_name).is_file() for d in Driver.DRIVER_INFOS))")
DEVICE_STATUS = DATA / "device-status.json"
KEYSTORE = DATA / "bumble-keys.json"
RECORDINGS = DATA / "recordings"


def control_socket_path(data: Path = DATA) -> Path:
    """Unix socket paths are limited to about 104 bytes; long home paths use a private temp dir."""
    path = data / "bumble-control.sock"
    if len(str(path).encode()) < 100:
        return path
    return Path(tempfile.gettempdir()) / f"hearing-emulator-{os.getuid()}" / "control.sock"


CONTROL_SOCKET = control_socket_path()


def tool(binary: str) -> Path:
    return VENV / "bin" / binary


def installed() -> bool:
    return tool("python").exists() and tool("bumble-usb-probe").exists()


def install(force: bool = False) -> None:
    ensure_unprivileged()
    if sys.version_info < (3, 11):
        raise ValueError("Use Python 3.11+ to install current Bumble releases")
    if is_ubuntu():
        execute(["sudo", "apt-get", "install", "-y", "python3-venv", "libusb-1.0-0"])
    if not VENV.exists():
        execute([sys.executable, "-m", "venv", str(VENV)])
    if installed() and not force:
        ok("Bumble is already installed in the isolated virtual environment")
    else:
        execute([str(tool("python")), "-m", "pip", "install", "--upgrade", "pip"])
        execute([str(tool("python")), "-m", "pip", "install", "--upgrade", "bumble"])
        if not installed():
            raise RuntimeError("Bumble installed but its USB probe command was not found")
        ok("Bumble installed (ASHA, A2DP and HFP profiles)")
    if _realtek_info()[1] and not force:
        ok("Realtek dongle firmware already downloaded")
        return
    result = capture([str(tool("python")), "-c", REALTEK_DOWNLOAD], timeout=300)
    downloaded = _realtek_info()[1]
    if result.returncode == 0 and downloaded:
        ok(f"Downloaded {downloaded} Realtek dongle firmware images")
    else:
        warn(f"Realtek firmware download failed: {(result.stderr or result.stdout).strip()[-300:]}")


def _realtek_info() -> tuple[set[str], int]:
    """USB IDs of Realtek dongles Bumble drives, and the number of firmware images on disk."""
    lines = capture([str(tool("python")), "-c", REALTEK_STATUS]).stdout.splitlines()
    if len(lines) != 2 or not lines[1].isdigit():
        return set(), 0
    return set(lines[0].split()), int(lines[1])


def asha_identity(config: Config) -> tuple[str, str]:
    """Stable HiSyncId and static random address, so the phone's bond survives restarts.

    Without an explicit sync ID one is derived from name and side. Changing the name,
    side or sync ID therefore presents a new device that the phone must pair again.
    """
    sync_id = (config.asha_sync_id
               or hashlib.sha256(f"sync/{config.name}/{config.asha_side}".encode()).hexdigest()[:16])
    address = bytearray(hashlib.sha256(f"address/{sync_id.upper()}/{config.asha_side}".encode()).digest()[:6])
    address[0] |= 0xC0  # static random address: two most significant bits set
    return sync_id.upper(), ":".join(f"{octet:02X}" for octet in address)


def commands(config: Config) -> list[tuple[str, list[str]]]:
    """One worker presents every configured profile on a single adapter."""
    sync_id, address = asha_identity(config)
    transport = config.bumble_transport
    if config.microphone and transport.startswith("usb:") and "+sco=" not in transport:
        transport += "+sco=0"  # carry SCO call audio over the adapter's isochronous USB endpoints
    command = [str(tool("python")), "-u", "-m", "hearing_emulator.workers.bumble_device",
               "--transport", transport, "--name", config.name, "--profiles", ",".join(config.profiles),
               "--address", address, "--codec", config.bumble_codec, "--keystore", str(KEYSTORE),
               "--status-file", str(DEVICE_STATUS), "--control-socket", str(CONTROL_SOCKET)]
    if "asha-sink" in config.profiles:
        command.extend(["--side", config.asha_side, "--sync-id", sync_id])
        if config.asha_binaural:
            command.append("--binaural")
    if config.microphone:
        command.extend(["--mic-source", config.mic_source, "--mic-frequency", str(config.mic_frequency),
                        "--mic-gain", str(config.mic_gain)])
        if config.mic_source == "wav":
            command.extend(["--mic-wave-path", config.mic_wave_path])
    if config.record_audio:
        command.extend(["--record-dir", str(RECORDINGS)])
    if config.playback_enabled and has("ffplay"):
        command.append("--play")
    return [("device", command)]


def control(command: dict, path: Path = CONTROL_SOCKET, timeout: float = 45) -> dict:
    """Send one command to the running worker (see workers/bumble_device/device.py)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall(json.dumps(command).encode() + b"\n")
            response = b""
            while not response.endswith(b"\n"):
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise RuntimeError("The Bumble emulator is not running; use 'start' first") from exc
    return json.loads(response)


def doctor(config: Config) -> list[tuple[bool, str]]:
    results = [(sys.version_info >= (3, 11), "Python 3.11+"),
               (sys.platform in {"darwin", "linux"}, "Supported Bumble host (macOS/Linux)"),
               (installed(), "Bumble environment and USB probe installed")]
    if installed():
        # --hci-only: without it every USB device (keyboard, hub...) gets a usb: name.
        result = capture([str(tool("bumble-usb-probe")), "--hci-only"], timeout=12)
        output = result.stdout.upper()
        results.append((result.returncode == 0, "USB probe executed"))
        results.append(("TRANSPORT NAMES" in output, "Bluetooth USB controller found by bumble-usb-probe"))
        results.append((config.bumble_transport.upper() in output,
                        f"Configured transport {config.bumble_transport} is listed by the probe"))
        realtek_ids, firmware_images = _realtek_info()
        attached = sorted(usb_id for usb_id in realtek_ids if f"USB:{usb_id}" in output)
        if attached:
            results.append((firmware_images > 0, f"Realtek firmware downloaded for {', '.join(attached)} "
                                                 "(fix: install --backend bumble)"))
        probe = capture([str(tool("python")), "-c", "import bumble.profiles.asha, bumble.a2dp, bumble.hfp"])
        results.append((probe.returncode == 0, "Bumble ASHA, A2DP and HFP profiles importable"))
    if config.microphone and config.mic_source == "wav":
        from ..workers.microphone import validate_wave
        try:
            validate_wave(Path(config.mic_wave_path), config.mic_sample_rate)
        except (ValueError, OSError) as exc:
            results.append((False, f"Microphone WAV file: {exc}"))
        else:
            results.append((True, "Microphone WAV format and path valid"))
    return results
