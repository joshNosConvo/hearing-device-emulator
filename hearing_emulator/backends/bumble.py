"""Bumble backend: USB-attached A2DP sink or ASHA hearing device on macOS or Linux."""
from __future__ import annotations

import hashlib
from pathlib import Path
import json
import sys

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
ASHA_STATUS = DATA / "asha-status.json"
ASHA_KEYSTORE = DATA / "bumble-asha-keys.json"
RECORDINGS = DATA / "recordings"


def tool(binary: str) -> Path:
    return VENV / "bin" / binary


def installed() -> bool:
    return tool("bumble-speaker").exists() and tool("bumble-usb-probe").exists()


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
            raise RuntimeError("Bumble installed but speaker/probe command was not found")
        ok("Bumble A2DP speaker and ASHA runtime installed")
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
    if config.microphone:
        raise ValueError("Bumble backend does not implement the HFP microphone")
    if config.mode == "asha":
        sync_id, address = asha_identity(config)
        command = [str(tool("python")), "-u", "-m", "hearing_emulator.workers.bumble_asha",
                   "--transport", config.bumble_transport, "--name", config.name,
                   "--side", config.asha_side, "--sync-id", sync_id, "--address", address,
                   "--keystore", str(ASHA_KEYSTORE), "--status-file", str(ASHA_STATUS)]
        if config.asha_binaural:
            command.append("--binaural")
        if config.record_audio:
            command.extend(["--record-dir", str(RECORDINGS)])
        if config.playback_enabled and has("ffplay"):
            command.append("--play")
        return [("asha", command)]
    if config.mode != "a2dp":
        raise ValueError("Bumble backend supports a2dp and asha modes")
    device_config = DATA / "bumble-device.json"
    device_config.parent.mkdir(parents=True, exist_ok=True)
    device_config.write_text(json.dumps({"name": config.name, "class_of_device": 0x240414,
                                         "keystore": "JsonKeyStore", "classic_enabled": True,
                                         "le_enabled": False}, indent=2) + "\n", encoding="utf-8")
    command = [str(tool("bumble-speaker")), "--codec", config.bumble_codec,
               "--ui-port", str(config.bumble_ui_port),
               "--device-config", str(device_config)]
    # With no --output, Bumble serves an audio graph and optional browser playback.
    # @ffplay adds native host playback only if ffplay is present.
    if config.playback_enabled and has("ffplay"):
        command.extend(["--output", "@ffplay"])
    command.append(config.bumble_transport)
    return [("speaker", command)]


def doctor(config: Config) -> list[tuple[bool, str]]:
    results = [(sys.version_info >= (3, 11), "Python 3.11+"),
               (sys.platform in {"darwin", "linux"}, "Supported Bumble host (macOS/Linux)"),
               (installed(), "Bumble speaker and USB probe installed")]
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
        if config.mode == "asha":
            probe = capture([str(tool("python")), "-c", "import bumble.profiles.asha"])
            results.append((probe.returncode == 0, "Bumble ASHA profile importable"))
    return results
