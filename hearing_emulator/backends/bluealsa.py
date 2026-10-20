"""BlueALSA 5.0.0 backend: Linux ASHA/A2DP/HFP receiver."""
from __future__ import annotations

import getpass
import grp
from pathlib import Path
import os
import pwd
import sys
import tempfile
from xml.etree import ElementTree

from ..config import ASHA_MODES, Config
from ..ops import DATA, capture, ensure_unprivileged, execute, has, info, is_ubuntu, ok, warn

SOURCE = DATA / "sources" / "bluez-alsa"
TAG = "v5.0.0"
DEPENDENCIES = [
    "git", "automake", "build-essential", "libtool", "pkg-config", "python3-docutils",
    "libasound2-dev", "libbluetooth-dev", "libdbus-1-dev", "libglib2.0-dev",
    "libsbc-dev", "libspandsp-dev", "bluez", "usbutils", "rfkill", "alsa-utils",
]
# By default only root may own org.bluealsa, and only the "audio" group may talk to it.
# This drop-in lets the invoking user run bluealsad and bluealsa-aplay unprivileged.
DBUS_POLICY_DIRS = [Path("/etc/dbus-1/system.d"), Path("/usr/share/dbus-1/system.d")]
DBUS_POLICY_FILE = DBUS_POLICY_DIRS[0] / "hearing-emulator-bluealsa.conf"
DBUS_POLICY = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<!-- Installed by hearing-emulator: lets {user} run bluealsad without root. -->
<busconfig>
  <policy user="{user}">
    <allow own_prefix="org.bluealsa"/>
    <allow send_destination="org.bluealsa"/>
  </policy>
</busconfig>
"""
WIREPLUMBER_CONFIG_DIRS = [Path.home() / ".config" / "wireplumber", Path("/etc/wireplumber")]


def installed() -> bool:
    if not (has("bluealsad") and has("bluealsa-aplay")):
        return False
    version = capture(["bluealsad", "--version"])
    help_text = capture(["bluealsad", "--help"])
    output = version.stdout + version.stderr
    return version.returncode == 0 and "5.0.0" in output and "asha-sink" in (
        help_text.stdout + help_text.stderr
    )


def install(force: bool = False) -> None:
    ensure_unprivileged()
    if not is_ubuntu():
        raise ValueError("BlueALSA automated installer supports Ubuntu only")
    if installed() and not force:
        ok("BlueALSA 5.0.0 with ASHA is already installed. Skipping build")
    else:
        _build(force)
    _install_dbus_policy()


def _build(force: bool) -> None:
    execute(["sudo", "apt-get", "update"])
    execute(["sudo", "apt-get", "install", "-y", *DEPENDENCIES])
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    if SOURCE.exists():
        if not (SOURCE / ".git").exists():
            raise ValueError(f"Directory exists but isn't a Git repository: {SOURCE}")
        if force:
            execute(["git", "fetch", "--tags"], cwd=SOURCE)
            execute(["git", "checkout", TAG], cwd=SOURCE)
    else:
        execute(["git", "clone", "--branch", TAG, "--depth", "1",
                 "https://github.com/arkq/bluez-alsa.git", str(SOURCE)])
    execute(["autoreconf", "--install", "--force"], cwd=SOURCE)
    build = SOURCE / "build"
    build.mkdir(exist_ok=True)
    configure = [str(SOURCE / "configure"), "--enable-asha", "--enable-msbc",
                 "--enable-debug", "--enable-manpages"]
    execute(configure, cwd=build)
    execute(["make", "-j2"], cwd=build)
    execute(["sudo", "make", "install"], cwd=build)
    execute(["sudo", "ldconfig"])
    if not installed():
        warn("Build finished but BlueALSA was not found on PATH. Check /usr/local/bin")
    else:
        ok("BlueALSA installed with ASHA, A2DP, and HFP support")


def _install_dbus_policy() -> None:
    user = getpass.getuser()
    if dbus_policy_allows(user):
        ok(f"D-Bus policy already lets {user} run bluealsad")
        return
    info(f"Installing {DBUS_POLICY_FILE} so {user} can run bluealsad without root")
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as draft:
        draft.write(DBUS_POLICY.format(user=user))
    try:
        execute(["sudo", "install", "-m", "0644", draft.name, str(DBUS_POLICY_FILE)])
    finally:
        os.unlink(draft.name)
    # dbus-daemon watches system.d itself; an explicit reload covers dbus-broker.
    if capture(["sudo", "systemctl", "reload", "dbus"], timeout=20).returncode != 0:
        warn("Could not reload D-Bus; log out and in (or reboot) before 'start'")


def dbus_policy_allows(user: str, directories: list[Path] | None = None) -> bool:
    """True if a system bus policy lets USER own org.bluealsa (needed to run bluealsad)."""
    try:
        record = pwd.getpwnam(user)
        groups = {grp.getgrgid(gid).gr_name for gid in os.getgrouplist(user, record.pw_gid)}
    except KeyError:
        return False
    return any(_grants_bluealsa_owner(path, user, groups)
               for directory in directories or DBUS_POLICY_DIRS
               for path in sorted(directory.glob("*.conf")))


def _grants_bluealsa_owner(path: Path, user: str, groups: set[str]) -> bool:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return False
    for policy in root.iter("policy"):
        if policy.get("user") not in (user, "*") and policy.get("group") not in groups:
            continue
        if any("org.bluealsa" in (allow.get("own"), allow.get("own_prefix"))
               for allow in policy.iter("allow")):
            return True
    return False


def wireplumber_bluez_disabled(directories: list[Path] | None = None) -> bool:
    """True if a WirePlumber 0.4 or 0.5 override turns off its Bluetooth monitor."""
    for directory in directories or WIREPLUMBER_CONFIG_DIRS:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            try:
                text = path.read_text(encoding="utf-8") if path.is_file() else ""
            except (OSError, UnicodeDecodeError):
                continue
            lines = [line.split("--")[0].split("#")[0] for line in text.splitlines()]
            code = "\n".join(lines).replace(" ", "")
            if "monitor.bluez=disabled" in code or "bluez_monitor.enabled=false" in code:
                return True
    return False


def competing_audio_stacks() -> list[str]:
    """Other Bluetooth audio servers registering the same BlueZ profiles as bluealsad."""
    def running(name: str) -> bool:
        return capture(["pgrep", "-x", name], timeout=5).returncode == 0

    found = []
    if running("bluealsad") or running("bluealsa"):
        found.append("a BlueALSA daemon is already running (system service, or 'stop' this tool)")
    if running("wireplumber") and not wireplumber_bluez_disabled():
        found.append("WirePlumber Bluetooth monitor (PipeWire) is enabled")
    if running("pulseaudio") and "module-bluetooth" in capture(["pactl", "list", "modules", "short"]).stdout:
        found.append("PulseAudio module-bluetooth is loaded")
    return found


def commands(config: Config) -> list[tuple[str, list[str]]]:
    # No --loglevel: when omitted bluealsad logs everything its build supports,
    # while --loglevel=debug is rejected by builds without --enable-debug.
    daemon = ["bluealsad", f"--device={config.hci_device}"]
    for profile in config.profiles:
        daemon.append(f"--profile={profile}")
    if config.mode in ASHA_MODES:
        side = config.asha_side + ("+" if config.asha_binaural else "")
        daemon.extend(["--asha-advertise", f"--asha-adv-name={config.name}", f"--asha-side={side}"])
        if config.asha_sync_id:
            daemon.append(f"--asha-sync-id={config.asha_sync_id}")
    result = [("daemon", daemon)]
    if config.playback_enabled:
        listener = ["bluealsa-aplay", f"--pcm={config.alsa_pcm}"]
        if config.mode in ASHA_MODES:
            listener.append("--profile=ASHA")
        if config.mode in ("a2dp", "cross-platform"):
            listener.append("--profile=A2DP")
        if config.microphone:
            listener.append("--profile=SCO")
        result.append(("audio", listener))
    if config.microphone:
        microphone = [sys.executable, "-u", "-m", "hearing_emulator.workers.microphone",
                      "--source", config.mic_source,
                      "--sample-rate", str(config.mic_sample_rate),
                      "--frequency", str(config.mic_frequency),
                      "--gain", str(config.mic_gain)]
        if config.mic_source == "wav":
            microphone.extend(["--wave-path", config.mic_wave_path])
        elif config.mic_source == "alsa":
            microphone.extend(["--alsa-device", config.mic_alsa_device])
        if config.mic_peer:
            microphone.extend(["--peer", config.mic_peer])
        result.append(("microphone", microphone))
    return result


def doctor(config: Config) -> list[tuple[bool, str]]:
    results: list[tuple[bool, str]] = []
    results.append((is_ubuntu(), "Ubuntu/Linux runtime (BlueALSA requires Linux)"))
    results.append((installed(), "BlueALSA 5.0.0 with ASHA build"))
    user = getpass.getuser()
    results.append((dbus_policy_allows(user),
                    f"D-Bus policy lets {user} run bluealsad (fix: install --backend bluealsa)"))
    competitors = competing_audio_stacks()
    results.append((not competitors, "No competing Bluetooth audio stack"
                    + (f": {'; '.join(competitors)} (see README)" if competitors else "")))
    results.append((has("bluetoothctl"), "BlueZ bluetoothctl command"))
    if has("lsusb"):
        result = capture(["lsusb"])
        results.append((result.returncode == 0, "USB bus can be inspected"))
    detected = Path("/sys/class/bluetooth")
    results.append((detected.exists(), "Bluetooth kernel subsystem present"))
    if has("bluetoothctl"):
        listed = capture(["bluetoothctl", "list"], timeout=8)
        controllers = "Controller " in listed.stdout
        results.append((controllers, "Physical Bluetooth controller visible to BlueZ"))
    if config.playback_enabled:
        results.append((has("aplay"), "ALSA utilities installed (not a playback guarantee)"))
    if config.microphone:
        results.append((has("arecord") and has("aplay"), "ALSA microphone tools (arecord/aplay)"))
        if config.mic_source == "wav":
            from ..workers.microphone import validate_wave
            try:
                validate_wave(Path(config.mic_wave_path), config.mic_sample_rate)
            except (ValueError, OSError) as exc:
                results.append((False, f"Microphone WAV file: {exc}"))
            else:
                results.append((True, "Microphone WAV format and path valid"))
        results.append((has("bluealsa-aplay"), "BlueALSA PCM enumeration available for HFP"))
    return results
