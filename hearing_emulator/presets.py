"""Named device presets: which real hearing-device path a configuration imitates.

A preset only describes the device being pretended (protocols, ASHA identity,
microphone). The backend that realizes it stays a separate configuration choice,
validated against config.BACKEND_MODES.
"""
from __future__ import annotations

from dataclasses import dataclass

# What the phone's audio routing layer should report for each advertised profile.
# This is what the app under test observes, so QA can assert on it.
PROFILE_ROUTES = {
    "asha-sink": "Android AudioDeviceInfo.TYPE_HEARING_AID (iOS: not offered; iPhone needs MFi)",
    "a2dp-sink": "Android TYPE_BLUETOOTH_A2DP; iOS AVAudioSession.Port.bluetoothA2DP",
    "hfp-hf": "Android TYPE_BLUETOOTH_SCO; iOS AVAudioSession.Port.bluetoothHFP",
}


@dataclass(frozen=True)
class Preset:
    imitates: str
    settings: dict


PRESETS = {
    "cochlear-asha": Preset(
        "Cochlear processor (Nucleus 7/8, Kanso 2) streaming directly from Android over ASHA; "
        "single processor (monaural)",
        {"mode": "asha", "microphone": False, "asha_side": "left", "asha_binaural": False},
    ),
    "cochlear-asha-pair-left": Preset(
        "Left side of a bilateral (two implants) or bimodal (implant + hearing aid) ASHA pair. "
        "Run the right side on a second adapter/host with the same asha_sync_id",
        {"mode": "asha", "microphone": False, "asha_side": "left", "asha_binaural": True},
    ),
    "cochlear-asha-pair-right": Preset(
        "Right side of a bilateral or bimodal ASHA pair; use the left side's asha_sync_id",
        {"mode": "asha", "microphone": False, "asha_side": "right", "asha_binaural": True},
    ),
    "cochlear-phone-clip": Preset(
        "Phone Clip style accessory: a classic Bluetooth headset (A2DP audio + HFP microphone), "
        "not the processor's own direct-streaming path",
        {"mode": "a2dp", "microphone": True, "mic_source": "tone"},
    ),
    "a2dp-speaker": Preset(
        "Generic output-only Bluetooth speaker; what an iPhone falls back to without MFi",
        {"mode": "a2dp", "microphone": False},
    ),
}


def expected_routes(profiles: list[str]) -> list[str]:
    return [f"{profile}: {PROFILE_ROUTES[profile]}" for profile in profiles]
