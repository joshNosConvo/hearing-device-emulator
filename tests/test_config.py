import getpass
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hearing_emulator.cli import default_install_targets
from hearing_emulator.config import Config, read_config, save_config
from hearing_emulator.backends import bluealsa, bumble
from hearing_emulator.presets import PRESETS, PROFILE_ROUTES


class ConfigTests(unittest.TestCase):
    def test_profiles(self):
        self.assertEqual(Config(mode="asha").profiles, ["asha-sink"])
        self.assertEqual(Config(mode="a2dp").profiles, ["a2dp-sink"])
        self.assertEqual(Config(mode="cross-platform").profiles,
                         ["asha-sink", "a2dp-sink"])
        self.assertEqual(Config(mode="cross-platform", microphone=True).profiles,
                         ["asha-sink", "a2dp-sink", "hfp-hf"])

    def test_invalid_microphone_asha(self):
        with self.assertRaisesRegex(ValueError, "output-only"):
            Config(mode="asha", microphone=True).validate()

    def test_bumble_runs_every_mode_and_the_microphone(self):
        for mode in ("asha", "a2dp", "cross-platform"):
            Config(backend="bumble", mode=mode, record_audio=True).validate()
        Config(backend="bumble", mode="cross-platform", microphone=True, mic_source="tone").validate()

    def test_bumble_microphone_limits(self):
        with self.assertRaisesRegex(ValueError, "microphone sources are: silence, tone, wav"):
            Config(backend="bumble", microphone=True, mic_source="alsa").validate()
        with self.assertRaisesRegex(ValueError, "CVSD at 8000 Hz"):
            Config(backend="bumble", microphone=True, mic_sample_rate=16000).validate()
        with self.assertRaisesRegex(ValueError, "mic_peer applies to bluealsa only"):
            Config(backend="bumble", microphone=True, mic_peer="AA:BB:CC:DD:EE:FF").validate()

    def test_removed_bumble_ui_port_key_still_loads(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text('{"backend": "bumble", "bumble_ui_port": 7654}')
            self.assertEqual(read_config(path).backend, "bumble")

    def test_asha_identity_validation(self):
        Config(mode="asha", asha_side="right", asha_sync_id="0123456789ABCDEF").validate()
        with self.assertRaisesRegex(ValueError, "asha_side"):
            Config(mode="asha", asha_side="center").validate()
        with self.assertRaisesRegex(ValueError, "16 hexadecimal"):
            Config(mode="asha", asha_sync_id="12345").validate()
        with self.assertRaisesRegex(ValueError, "shared by both sides"):
            Config(mode="asha", asha_binaural=True).validate()

    def test_config_persistence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            expected = Config(mode="cross-platform", name="Test-Lab")
            save_config(expected, path)
            self.assertEqual(read_config(path), expected)

    def test_bluealsa_build_command(self):
        config = Config(mode="cross-platform", microphone=True, name="QA-Lab")
        commands = bluealsa.commands(config)
        daemon = commands[0][1]
        self.assertIn("--profile=asha-sink", daemon)
        self.assertIn("--profile=a2dp-sink", daemon)
        self.assertIn("--profile=hfp-hf", daemon)
        self.assertIn("--asha-advertise", daemon)
        self.assertIn("--asha-adv-name=QA-Lab", daemon)
        self.assertIn("--asha-side=left", daemon)
        self.assertFalse(any(arg.startswith("--loglevel") for arg in daemon))
        self.assertIn("--profile=SCO", commands[1][1])

    def test_bluealsa_binaural_asha_identity(self):
        config = Config(mode="asha", asha_side="right", asha_binaural=True,
                        asha_sync_id="00112233AABBCCDD")
        daemon = bluealsa.commands(config)[0][1]
        self.assertIn("--asha-side=right+", daemon)
        self.assertIn("--asha-sync-id=00112233AABBCCDD", daemon)

    def test_bluealsa_pure_a2dp_no_mic(self):
        command = bluealsa.commands(Config())[0][1]
        self.assertIn("--profile=a2dp-sink", command)
        self.assertNotIn("--profile=hfp-hf", command)
        self.assertNotIn("--asha-advertise", command)
        self.assertFalse(any(arg.startswith("--asha-side") for arg in command))

    def test_bumble_runs_one_device_worker_for_all_profiles(self):
        config = Config(backend="bumble", mode="cross-platform", name="CI-Right", asha_side="right",
                        asha_binaural=True, asha_sync_id="00112233aabbccdd", microphone=True,
                        mic_source="tone", bumble_codec="aac", record_audio=True)
        (label, cmd), = bumble.commands(config)
        self.assertEqual(label, "device")
        self.assertEqual(cmd[1:4], ["-u", "-m", "hearing_emulator.workers.bumble_device"])
        for flag, value in (("--profiles", "asha-sink,a2dp-sink,hfp-hf"), ("--side", "right"),
                            ("--sync-id", "00112233AABBCCDD"), ("--name", "CI-Right"), ("--codec", "aac"),
                            ("--mic-source", "tone"), ("--transport", "usb:0+sco=0")):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertIn("--binaural", cmd)
        self.assertIn("--record-dir", cmd)
        self.assertEqual(cmd[cmd.index("--status-file") + 1], str(bumble.DEVICE_STATUS))
        self.assertEqual(cmd[cmd.index("--control-socket") + 1], str(bumble.CONTROL_SOCKET))

    def test_control_socket_falls_back_for_long_home_paths(self):
        self.assertEqual(bumble.control_socket_path(Path("/Users/qa/.hearing-emulator")),
                         Path("/Users/qa/.hearing-emulator/bumble-control.sock"))
        fallback = bumble.control_socket_path(Path("/" + "x" * 120))
        self.assertLess(len(str(fallback)), 100)
        self.assertEqual(fallback.name, "control.sock")

    def test_bumble_sco_transport_only_with_microphone(self):
        plain = bumble.commands(Config(backend="bumble", mode="a2dp"))[0][1]
        self.assertEqual(plain[plain.index("--transport") + 1], "usb:0")
        self.assertNotIn("--side", plain)  # ASHA identity only when asha-sink is offered
        chosen = Config(backend="bumble", microphone=True, bumble_transport="usb:0B05:190E+sco=2")
        cmd = bumble.commands(chosen)[0][1]
        self.assertEqual(cmd[cmd.index("--transport") + 1], "usb:0B05:190E+sco=2")

    def test_bumble_asha_identity_is_stable_static_random(self):
        left = Config(backend="bumble", mode="asha", name="CI")
        sync_id, address = bumble.asha_identity(left)
        self.assertEqual((sync_id, address), bumble.asha_identity(left))  # bond survives restarts
        self.assertRegex(sync_id, r"^[0-9A-F]{16}$")
        self.assertGreaterEqual(int(address[:2], 16), 0xC0)  # static random: top bits 11
        right = replace(left, asha_side="right", asha_binaural=True, asha_sync_id=sync_id)
        right_sync_id, right_address = bumble.asha_identity(right)
        self.assertEqual(right_sync_id, sync_id)
        self.assertNotEqual(right_address, address)

    def test_record_audio_only_for_bumble(self):
        with self.assertRaisesRegex(ValueError, "record_audio"):
            Config(backend="bluealsa", mode="asha", record_audio=True).validate()

    def test_doctor_checks_firmware_for_any_known_realtek_dongle(self):
        probe = "Bumble Transport Names: usb:0 or usb:2357:0604\n"  # TP-Link UB500, not the BT500

        def fake_capture(cmd, timeout=12):
            if "--hci-only" in cmd:
                return subprocess.CompletedProcess(cmd, 0, probe, "")
            if cmd[-1] == bumble.REALTEK_STATUS:
                return subprocess.CompletedProcess(cmd, 0, "0B05:190E 2357:0604\n0\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(bumble, "installed", return_value=True), \
                patch.object(bumble, "capture", fake_capture):
            checks = {label: passed for passed, label in bumble.doctor(Config(backend="bumble"))}
        firmware = [label for label in checks if label.startswith("Realtek firmware")]
        self.assertEqual(len(firmware), 1)
        self.assertIn("2357:0604", firmware[0])
        self.assertFalse(checks[firmware[0]])

    def test_install_defaults_to_bumble_off_linux(self):
        self.assertEqual(default_install_targets("darwin"), ["bumble"])
        self.assertEqual(default_install_targets("linux"), ["bluealsa", "bumble"])


class PresetTests(unittest.TestCase):
    def test_every_preset_is_a_valid_bluealsa_config(self):
        for name, preset in PRESETS.items():
            with self.subTest(preset=name):
                config = replace(Config(), **preset.settings)
                if config.asha_binaural:
                    config = replace(config, asha_sync_id="0123456789ABCDEF")
                config.validate()
                bluealsa.commands(config)
                for profile in config.profiles:
                    self.assertIn(profile, PROFILE_ROUTES)

    def test_every_preset_runs_on_bumble(self):
        for name, preset in PRESETS.items():
            with self.subTest(preset=name):
                config = replace(Config(backend="bumble"), **preset.settings)
                config = replace(config, asha_sync_id="0123456789ABCDEF")
                config.validate()
                command = bumble.commands(config)[0][1]
                self.assertEqual(command[command.index("--profiles") + 1], ",".join(config.profiles))

    def test_cochlear_asha_is_output_only_hearing_aid(self):
        config = replace(Config(), **PRESETS["cochlear-asha"].settings)
        self.assertEqual(config.profiles, ["asha-sink"])
        self.assertIn("TYPE_HEARING_AID", PROFILE_ROUTES["asha-sink"])


class BlueAlsaHostChecks(unittest.TestCase):
    def test_dbus_policy_detection(self):
        user = getpass.getuser()
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            self.assertFalse(bluealsa.dbus_policy_allows(user, [directory]))
            (directory / "org.bluealsa.conf").write_text(
                '<busconfig><policy user="root"><allow own_prefix="org.bluealsa"/></policy></busconfig>')
            self.assertEqual(bluealsa.dbus_policy_allows(user, [directory]), user == "root")
            (directory / "hearing-emulator-bluealsa.conf").write_text(
                bluealsa.DBUS_POLICY.format(user=user))
            self.assertTrue(bluealsa.dbus_policy_allows(user, [directory]))

    def test_wireplumber_override_detection(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            self.assertFalse(bluealsa.wireplumber_bluez_disabled([directory]))
            (directory / "commented.lua").write_text("-- bluez_monitor.enabled = false\n")
            self.assertFalse(bluealsa.wireplumber_bluez_disabled([directory]))
            conf = directory / "wireplumber.conf.d" / "80-disable-bluez.conf"
            conf.parent.mkdir()
            conf.write_text("wireplumber.profiles = {\n  main = {\n    monitor.bluez = disabled\n  }\n}\n")
            self.assertTrue(bluealsa.wireplumber_bluez_disabled([directory]))


if __name__ == "__main__":
    unittest.main()
