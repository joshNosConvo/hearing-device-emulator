"""Validated, dependency-free JSON configuration."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path
import re

HOME = Path.home() / ".hearing-emulator"
CONFIG_FILE = HOME / "config.json"
MODES = ("asha", "a2dp", "cross-platform")
MIC_SOURCES = ("tone", "silence", "wav", "alsa")
ASHA_SIDES = ("left", "right")
ASHA_MODES = {"asha", "cross-platform"}
# What each backend can realize today. Extending a backend means editing this table,
# not the validation code below.
BACKEND_MODES = {"bluealsa": set(MODES), "bumble": {"a2dp", "asha"}}
BACKEND_MICROPHONE = {"bluealsa"}


@dataclass
class Config:
    backend: str = "bluealsa"
    mode: str = "a2dp"
    microphone: bool = False
    mic_source: str = "tone"  # tone | silence | wav | alsa
    mic_wave_path: str = ""
    mic_alsa_device: str = "default"
    mic_peer: str = ""  # optional Bluetooth address, otherwise most recent SCO device
    mic_sample_rate: int = 8000  # baseline CVSD; 16000 for wideband HFP
    mic_frequency: float = 440.0
    mic_gain: float = 0.2
    name: str = "Hearing-Emulator"
    asha_side: str = "left"  # left | right
    asha_binaural: bool = False  # one side of a bilateral/bimodal pair
    asha_sync_id: str = ""  # HiSyncId, 16 hex digits; both sides of a pair must share it
    hci_device: str = "hci0"
    bumble_transport: str = "usb:0"
    bumble_codec: str = "sbc"
    playback_enabled: bool = True
    record_audio: bool = False  # save each received stream as WAV (Bumble ASHA only)
    alsa_pcm: str = "default"
    bumble_ui_port: int = 7654

    def validate(self) -> None:
        if self.backend not in BACKEND_MODES:
            raise ValueError(f"Unsupported backend: {self.backend}")
        if self.mode not in MODES:
            raise ValueError(f"Unsupported mode: {self.mode}")
        if self.microphone and self.mode == "asha":
            raise ValueError("Pure ASHA is output-only: microphone requires a Classic HFP profile")
        if self.mode not in BACKEND_MODES[self.backend]:
            supported = ", ".join(sorted(BACKEND_MODES[self.backend]))
            raise ValueError(f"The {self.backend} backend only implements mode(s): {supported}. "
                             f"Use bluealsa for {self.mode}")
        if self.microphone and self.backend not in BACKEND_MICROPHONE:
            raise ValueError(f"HFP microphone transmission is not implemented for the {self.backend} backend")
        if self.record_audio and (self.backend, self.mode) != ("bumble", "asha"):
            raise ValueError("record_audio is implemented only for the bumble backend in asha mode")
        if self.mic_source not in MIC_SOURCES:
            raise ValueError(f"mic_source must be one of: {', '.join(MIC_SOURCES)}")
        if self.microphone and self.mic_source == "wav" and not self.mic_wave_path:
            raise ValueError("mic_wave_path is required for microphone WAV source")
        if not self.mic_alsa_device or any(ch in self.mic_alsa_device for ch in "\n\r\0"):
            raise ValueError("mic_alsa_device must be a nonempty ALSA PCM name")
        if self.mic_sample_rate not in (8000, 16000):
            raise ValueError("mic_sample_rate must be 8000 or 16000")
        if not (20 <= self.mic_frequency <= 3500):
            raise ValueError("mic_frequency must be 20..3500 Hz")
        if not (0 <= self.mic_gain <= 1):
            raise ValueError("mic_gain must be 0..1")
        if self.mic_peer and not re.fullmatch(r"(?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}", self.mic_peer):
            raise ValueError("mic_peer must be a Bluetooth address (XX:XX:XX:XX:XX:XX)")
        if not self.name or len(self.name) > 24 or any(x in self.name for x in "\n\r\0"):
            raise ValueError("Device name must be 1–24 characters, without newlines")
        if self.asha_side not in ASHA_SIDES:
            raise ValueError("asha_side must be left or right")
        if self.asha_sync_id and not re.fullmatch(r"[0-9A-Fa-f]{16}", self.asha_sync_id):
            raise ValueError("asha_sync_id must be 16 hexadecimal digits (64-bit HiSyncId)")
        if self.asha_binaural and self.mode in ASHA_MODES and not self.asha_sync_id:
            raise ValueError("Binaural ASHA needs an asha_sync_id shared by both sides of the pair")
        if not self.hci_device.startswith("hci") or not self.hci_device[3:].isdigit():
            raise ValueError("hci_device must look like hci0")
        if not self.bumble_transport.startswith(("usb:", "pyusb:")):
            raise ValueError("Use a Bumble USB transport (e.g. usb:0, or usb:VID:PID from bumble-usb-probe)")
        if self.bumble_codec not in {"sbc", "aac"}:
            raise ValueError("bumble_codec must be sbc or aac")
        if not 1024 <= self.bumble_ui_port <= 65535:
            raise ValueError("bumble_ui_port must be between 1024 and 65535")
        if not self.alsa_pcm or any(ch in self.alsa_pcm for ch in "\n\r\0"):
            raise ValueError("alsa_pcm must not be empty or contain newlines")

    @property
    def profiles(self) -> list[str]:
        profiles = {
            "a2dp": ["a2dp-sink"],
            "asha": ["asha-sink"],
            "cross-platform": ["asha-sink", "a2dp-sink"],
        }[self.mode].copy()
        if self.microphone:
            profiles.append("hfp-hf")
        return profiles


def read_config(path: Path = CONFIG_FILE) -> Config:
    if not path.exists():
        config = Config()
    else:
        obj = json.loads(path.read_text(encoding="utf-8"))
        valid_fields = {f.name for f in fields(Config)}
        unknown = set(obj) - valid_fields
        if unknown:
            raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        config = Config(**obj)
    config.validate()
    return config


def save_config(config: Config, path: Path = CONFIG_FILE) -> None:
    config.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
