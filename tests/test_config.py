import getpass
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

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

    def test_bumble_rejects_asha(self):
        with self.assertRaisesRegex(ValueError, "bumble backend only implements mode"):
            Config(backend="bumble", mode="cross-platform").validate()

    def test_bumble_rejects_microphone(self):
        with self.assertRaisesRegex(ValueError, "not implemented"):
            Config(backend="bumble", microphone=True).validate()

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

    def test_bumble_a2dp_command(self):
        with tempfile.TemporaryDirectory() as d:
            old = bumble.DATA
            bumble.DATA = Path(d)
            try:
                cmd = bumble.commands(Config(backend="bumble", name="Speaker-Lab"))[0][1]
                self.assertIn("--device-config", cmd)
                self.assertIn("usb:0", cmd)
                self.assertIn("--codec", cmd)
                self.assertIn("Speaker-Lab", (Path(d) / "bumble-device.json").read_text())
            finally:
                bumble.DATA = old

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
