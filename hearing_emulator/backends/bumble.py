"""Bumble backend: USB-attached A2DP sink on macOS or Linux."""
from __future__ import annotations

from pathlib import Path
import json
import sys

from ..config import Config
from ..ops import DATA, capture, ensure_unprivileged, execute, has, ok, is_ubuntu, warn

VENV = DATA / "venv-bumble"
# Realtek dongles run on firmware that Bumble's rtk driver uploads at power-on.
# USB VID:PID -> firmware base name understood by bumble-rtk-fw-download --single.
REALTEK_FIRMWARE = {"0B05:190E": "rtl8761bu"}  # ASUS USB-BT500 (RTL8761BUV)


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
        ok("Bumble A2DP speaker CLI installed")
    for firmware in sorted(set(REALTEK_FIRMWARE.values())):
        if _realtek_firmware_present(firmware):
            continue
        result = capture([str(tool("bumble-rtk-fw-download")), "--single", firmware], timeout=120)
        if result.returncode == 0:
            ok(f"Downloaded Realtek firmware {firmware} (ASUS USB-BT500)")
        else:
            warn(f"Realtek firmware {firmware} download failed: {(result.stderr or result.stdout).strip()[-300:]}")


def _realtek_firmware_present(base_name: str) -> bool:
    script = ("from bumble.drivers.rtk import rtk_firmware_dir; "
              f"print((rtk_firmware_dir() / '{base_name}_fw.bin').is_file())")
    return capture([str(tool("python")), "-c", script]).stdout.strip() == "True"


def commands(config: Config) -> list[tuple[str, list[str]]]:
    if config.mode != "a2dp" or config.microphone:
        raise ValueError("Bumble MVP supports only A2DP without microphone")
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
        for usb_id, firmware in REALTEK_FIRMWARE.items():
            if usb_id in output:
                results.append((_realtek_firmware_present(firmware),
                                f"Realtek firmware {firmware} for {usb_id} (fix: install --backend bumble)"))
    return results
