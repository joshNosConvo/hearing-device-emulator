"""User-facing CLI orchestrating installation, validation, and receiver execution."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import re
import secrets
import sys
import time
import subprocess

from .backends import bluealsa, bumble
from .config import (ASHA_MODES, ASHA_SIDES, CONFIG_FILE, MIC_SOURCES, MODES, Config,
                     read_config, save_config)
from .ops import capture, error, execute, info, ok, warn
from .presets import PRESETS, expected_routes
from . import session

BACKENDS = {"bluealsa": bluealsa, "bumble": bumble}
MAC_ADDRESS = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def wizard(config: Config) -> Config:
    def prompt(label: str, default: str) -> str:
        answer = input(f"{label} [{default}]: ").strip()
        return answer or default

    backend = prompt(f"Backend ({'/'.join(BACKENDS)})", config.backend).lower()
    mode = prompt(f"Mode ({'/'.join(MODES)})", config.mode).lower()
    mic = prompt("Microphone via HFP (yes/no)", "yes" if config.microphone else "no").lower()
    name = prompt("Device name (ASHA advertisement)", config.name)
    transport = prompt("Bumble USB transport", config.bumble_transport)
    hci_device = prompt("BlueALSA HCI controller", config.hci_device)
    asha_side = config.asha_side
    asha_binaural = config.asha_binaural
    if mode in ASHA_MODES:
        asha_side = prompt(f"ASHA side ({'/'.join(ASHA_SIDES)})", config.asha_side).lower()
        asha_binaural = prompt("Part of a two-sided ASHA pair (yes/no)",
                               "yes" if config.asha_binaural else "no").lower() in {"yes", "y", "true"}
    mic_source = config.mic_source
    mic_wave_path = config.mic_wave_path
    mic_alsa_device = config.mic_alsa_device
    if mic in {"yes", "y", "true"}:
        mic_source = prompt(f"Microphone source ({'/'.join(MIC_SOURCES)})", config.mic_source).lower()
        if mic_source == "wav":
            mic_wave_path = prompt("16-bit mono WAV path", config.mic_wave_path)
        if mic_source == "alsa":
            mic_alsa_device = prompt("Local ALSA microphone device", config.mic_alsa_device)
    return replace(config, backend=backend, mode=mode, microphone=mic in {"yes", "y", "true"},
                   name=name, bumble_transport=transport, hci_device=hci_device,
                   asha_side=asha_side, asha_binaural=asha_binaural,
                   mic_source=mic_source, mic_wave_path=mic_wave_path,
                   mic_alsa_device=mic_alsa_device)


def config_action(args: argparse.Namespace) -> None:
    config = read_config()
    if args.preset:
        config = replace(config, **PRESETS[args.preset].settings)
    if args.interactive:
        config = wizard(config)
    overrides = {}
    for key in ("backend", "mode", "name", "hci_device", "bumble_transport", "alsa_pcm", "bumble_ui_port",
                "mic_source", "mic_wave_path", "mic_alsa_device", "mic_peer", "mic_sample_rate",
                "mic_frequency", "mic_gain", "asha_side", "asha_sync_id"):
        val = getattr(args, key)
        if val is not None:
            overrides[key] = val
    if args.microphone is not None:
        overrides["microphone"] = args.microphone == "on"
    if args.playback is not None:
        overrides["playback_enabled"] = args.playback == "on"
    if args.asha_binaural is not None:
        overrides["asha_binaural"] = args.asha_binaural == "on"
    config = replace(config, **overrides)
    generated_sync_id = (config.asha_binaural and config.mode in ASHA_MODES
                         and not config.asha_sync_id)
    if generated_sync_id:
        config = replace(config, asha_sync_id=secrets.token_hex(8).upper())
    save_config(config)
    ok(f"Saved configuration: {CONFIG_FILE}")
    print(json.dumps(config.__dict__, indent=2))
    print(f"Profiles: {', '.join(config.profiles)}")
    if generated_sync_id:
        other = "right" if config.asha_side == "left" else "left"
        info(f"Generated ASHA sync ID {config.asha_sync_id}. Configure the {other} side with "
             f"--asha-sync-id {config.asha_sync_id} so Android pairs both as one device")
    if config.microphone:
        info(f"HFP SCO microphone worker configured: {config.mic_source} at {config.mic_sample_rate} Hz")
        warn("iOS may switch from A2DP to HFP. ASHA remains output-only.")


def presets_action(args: argparse.Namespace) -> None:
    for name, preset in PRESETS.items():
        settings = ", ".join(f"{key}={value}" for key, value in preset.settings.items())
        print(f"{name}\n  Imitates: {preset.imitates}\n  Sets: {settings}")
        print("  Phone should report: " + "; ".join(
            expected_routes(replace(Config(), **preset.settings).profiles)))
    print("\nApply with: configure --preset NAME [--backend ...]; explicit flags override the preset.")


def default_install_targets(platform: str = sys.platform) -> list[str]:
    return list(BACKENDS) if platform.startswith("linux") else ["bumble"]


def install_action(args: argparse.Namespace) -> None:
    if args.backend is None:
        targets = default_install_targets()
    else:
        targets = list(BACKENDS) if args.backend == "all" else [args.backend]
    if "bluealsa" in targets and sys.platform != "linux":
        raise ValueError("BlueALSA requires Ubuntu/Linux. On macOS use install --backend bumble")
    if "bluealsa" in targets and not args.yes:
        print("BlueALSA installer will run sudo apt-get and sudo make install.")
        if input("Continue? Type yes: ").strip().lower() != "yes":
            info("Installation cancelled")
            return
    for backend in targets:
        info(f"Installing {backend}...")
        BACKENDS[backend].install(force=args.rebuild)
    ok("Requested software installation completed; actual Bluetooth functionality is unverified")


def doctor_action(args: argparse.Namespace) -> None:
    config = read_config()
    backend_names = list(BACKENDS) if args.backend == "all" else [args.backend or config.backend]
    for backend_name in backend_names:
        backend = BACKENDS[backend_name]
        print(f"\n=== {backend_name.upper()} environment ===")
        for passed, label in backend.doctor(config):
            (ok if passed else warn)(label)
        if backend_name == "bluealsa":
            print("  Info: Linux controller must be passed into Ubuntu; macOS built-in Bluetooth is not enough.")
        else:
            print("  Info: Bumble USB control on macOS may require extra setup; USB probe alone isn't audio validation.")
    print("\nNote: these checks do not prove pairing, audio streaming, or microphone routing.")


def plan_action(args: argparse.Namespace) -> None:
    config = read_config()
    print("Configuration:", json.dumps(config.__dict__, indent=2))
    print("Advertised Bluetooth roles:", ", ".join(config.profiles))
    print("Phone audio route to expect:")
    for route in expected_routes(config.profiles):
        print(f"  {route}")
    for label, argv in BACKENDS[config.backend].commands(config):
        print(f"  [{label}] {' '.join(argv)}")
    if config.backend == "bluealsa":
        print_pairing_hint(config)
    else:
        print(f"Bumble browser status: http://127.0.0.1:{config.bumble_ui_port} (once speaker starts)")
    print("Plan only: commands have not been executed.")


def print_pairing_hint(config: Config) -> None:
    if config.mode == "asha":
        # A real Cochlear processor is LE-only; classic discoverability would let the
        # phone pair a classic profile instead of the ASHA hearing-aid route.
        print("Pairing: bluealsad advertises ASHA over LE by itself. Keep classic discovery off and pair")
        print("  from Android Settings > Connected devices > Pair new device (hearing aids are listed there).")
        print("  Agent for the bonding prompt (other terminal): bluetoothctl, then")
        print("  power on / agent NoInputNoOutput / default-agent / pairable on")
        return
    print("Pairing (another terminal): bluetoothctl, then power on / agent NoInputNoOutput /")
    print(f"  default-agent / pairable on / discoverable on / system-alias {config.name}")


def start_action(args: argparse.Namespace) -> None:
    config = read_config()
    backend = BACKENDS[config.backend]
    if not backend.installed():
        raise RuntimeError(f"{config.backend} is not installed. Run 'install --backend {config.backend}'")
    results = backend.doctor(config)
    failures = [label for passed, label in results if not passed]
    if failures and not args.force:
        warn("Start blocked by preflight issues:")
        for failure in failures:
            print(f"  - {failure}")
        print("Use --force only for debugging, not as proof of hardware readiness.")
        raise RuntimeError("Preflight failed")
    if failures:
        warn("Proceeding despite failed checks (--force); hardware operations may fail")
    info(f"Mode: {config.mode}, profiles: {', '.join(config.profiles)}")
    if config.microphone:
        info("Microphone worker will WAIT until a phone opens an HFP SCO route")
        warn("A running microphone process does not prove that the phone captured audio")
    state = session.start(config.backend, backend.commands(config))
    ok("Emulator processes are running. This does NOT mean a phone is connected.")
    for process in state["processes"]:
        print(f"  {process['label']}: PID {process['pid']}; log {process['log']}")
    if config.backend == "bluealsa":
        print_pairing_hint(config)
    else:
        print(f"Bumble UI: http://127.0.0.1:{config.bumble_ui_port}")
    print("Next: status | logs --tail 40 | stop")


def status_action(args: argparse.Namespace) -> None:
    state = session.status()
    if not state:
        print("No managed emulator session. Run 'start' after configuring a backend.")
        return
    print(f"Backend: {state['backend']}; started: {state.get('started', '?')}")
    for p in state.get("processes", []):
        print(f"  {p['label']}: {'RUNNING' if p['alive'] else 'STOPPED'} (PID {p['pid']})")
        print(f"    Log: {p['log']}")
        if p["label"] == "microphone":
            from .microphone import current_status
            mic_state = current_status()
            if mic_state.get("pid") == p["pid"] and p["alive"]:
                print(f"    Microphone state: {mic_state.get('state')} | {mic_state.get('detail')}")
            else:
                print("    Microphone state: no fresh status; inspect logs --component microphone")
    print("Process status is not proof that an Android/iPhone connection or audio stream is active.")


def connections_action(args: argparse.Namespace) -> None:
    config = read_config()
    if config.backend == "bluealsa":
        for argv in (["bluetoothctl", "devices", "Connected"], ["bluealsa-aplay", "--list-pcms"]):
            result = capture(argv, timeout=10)
            print("\n$", " ".join(argv))
            print((result.stdout + result.stderr).strip() or "(No devices or audio PCMs reported)")
    else:
        print("For Bumble, open the speaker web UI and examine its real-time connection/audio graph:")
        print(f"  http://127.0.0.1:{config.bumble_ui_port}")
        print("Or use logs --follow to inspect connection events.")
    print("Note: paired/connected status is not proof of active interpreter audio or correct microphone routing.")


def logs_action(args: argparse.Namespace) -> None:
    files = session.log_files()
    if args.component:
        files = [p for p in files if args.component in p.name]
    if not files:
        warn("No log files found. Run 'start' first.")
        return
    if args.follow:
        execute(["tail", "-n", str(args.tail), "-f", *map(str, files)])
        return
    for file in files:
        print(f"\n=== {file} ===")
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(lines[-args.tail:]))


def scenario_action(args: argparse.Namespace) -> None:
    config = read_config()
    if args.kind in {"disconnect", "connect"}:
        if config.backend != "bluealsa":
            raise ValueError("Direct disconnect/connect is available only through Linux BlueZ in this MVP")
        if not args.address or not MAC_ADDRESS.fullmatch(args.address):
            raise ValueError("Supply --address with a 6-byte Bluetooth MAC address")
        command = ["bluetoothctl", args.kind, args.address]
        result = capture(command, timeout=15)
        print((result.stdout + result.stderr).strip())
        if result.returncode:
            raise RuntimeError("Bluetooth command failed")
        info("The phone controls whether and when it actually reconnects or resumes streaming")
        return
    if args.kind == "restart":
        info("Stopping emulator processes to emulate receiver shutdown")
        session.stop()
        print(f"Waiting {args.delay}s before restoring availability...")
        time.sleep(args.delay)
        start_action(argparse.Namespace(force=args.force))
        return
    raise ValueError(f"Unknown scenario: {args.kind}")


def mic_sample_action(args: argparse.Namespace) -> None:
    from .microphone import generate_sample
    config = read_config()
    generate_sample(args.output, seconds=args.seconds, sample_rate=config.mic_sample_rate,
                    frequency=config.mic_frequency, gain=config.mic_gain)
    ok(f"Generated test WAV: {args.output} ({args.seconds}s, {config.mic_sample_rate} Hz)")
    info("Offline WAV generation does not prove Bluetooth microphone transmission")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hearing-emulator", description="Bluetooth audio QA emulator (MVP)")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("configure", help="Save receiver profile settings")
    setup.add_argument("--preset", choices=list(PRESETS),
                       help="Start from a named device preset (see 'presets'); other flags override it")
    setup.add_argument("--interactive", action="store_true")
    setup.add_argument("--backend", choices=list(BACKENDS))
    setup.add_argument("--mode", choices=MODES)
    setup.add_argument("--asha-side", choices=ASHA_SIDES)
    setup.add_argument("--asha-binaural", choices=["on", "off"],
                       help="This side is one half of a two-sided ASHA pair")
    setup.add_argument("--asha-sync-id", help="HiSyncId shared by both sides, 16 hex digits")
    setup.add_argument("--microphone", choices=["on", "off"])
    setup.add_argument("--mic-source", choices=MIC_SOURCES)
    setup.add_argument("--mic-wave-path")
    setup.add_argument("--mic-alsa-device")
    setup.add_argument("--mic-peer", help="Optional paired phone Bluetooth address")
    setup.add_argument("--mic-sample-rate", type=int, choices=[8000, 16000])
    setup.add_argument("--mic-frequency", type=float)
    setup.add_argument("--mic-gain", type=float)
    setup.add_argument("--name")
    setup.add_argument("--hci-device")
    setup.add_argument("--bumble-transport")
    setup.add_argument("--alsa-pcm")
    setup.add_argument("--bumble-ui-port", type=int)
    setup.add_argument("--playback", choices=["on", "off"])
    setup.set_defaults(func=config_action)
    inst = sub.add_parser("install", help="Install dependencies for one or both backends")
    inst.add_argument("--backend", choices=[*BACKENDS, "all"],
                      help="Default: all on Linux, bumble elsewhere")
    inst.add_argument("--rebuild", action="store_true", help="Rebuild or upgrade installed backend")
    inst.add_argument("--yes", action="store_true", help="Acknowledge system changes without prompt")
    inst.set_defaults(func=install_action)
    doctor = sub.add_parser("doctor", help="Inspect platform, packages, and Bluetooth controllers")
    doctor.add_argument("--backend", choices=[*BACKENDS, "all"])
    doctor.set_defaults(func=doctor_action)
    for name, method, help_text in [
        ("plan", plan_action, "Preview exact commands without running them"),
        ("presets", presets_action, "List device presets and the real device each imitates"),
        ("status", status_action, "Check running emulator processes"),
        ("connections", connections_action, "Inspect connected phones and audio PCMs"),
        ("stop", lambda args: session.stop(), "Stop managed emulator processes"),
    ]:
        cmd = sub.add_parser(name, help=help_text)
        cmd.set_defaults(func=method)
    start = sub.add_parser("start", help="Start configured receiver in background")
    start.add_argument("--force", action="store_true", help="Bypass failed preflight for debugging")
    start.set_defaults(func=start_action)
    logs = sub.add_parser("logs", help="Show BlueALSA/Bumble logs")
    logs.add_argument("--tail", type=int, default=40)
    logs.add_argument("--component", choices=["daemon", "audio", "speaker", "microphone"])
    logs.add_argument("--follow", action="store_true")
    logs.set_defaults(func=logs_action)
    demo = sub.add_parser("mic-sample", help="Generate test tone WAV offline, without Bluetooth hardware")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--seconds", type=float, default=3)
    demo.set_defaults(func=mic_sample_action)
    scenario = sub.add_parser("scenario", help="Run a connection test")
    scenario.add_argument("kind", choices=["disconnect", "connect", "restart"])
    scenario.add_argument("--address")
    scenario.add_argument("--delay", type=int, choices=range(1, 61), default=5,
                          metavar="1-60")
    scenario.add_argument("--force", action="store_true")
    scenario.set_defaults(func=scenario_action)
    return parser


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except (ValueError, RuntimeError, OSError, KeyboardInterrupt, subprocess.CalledProcessError) as exc:
        error(str(exc))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
