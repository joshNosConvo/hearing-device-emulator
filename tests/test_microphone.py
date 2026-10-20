"""Hardware-free tests for synthetic mic PCM and profile integration."""
from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import wave

from hearing_emulator import microphone
from hearing_emulator.backends.bluealsa import commands
from hearing_emulator.config import Config
from hearing_emulator.microphone import (
    generate_sample, has_sco_playback, pcm_device, pcm_silence, pcm_tone,
    validate_wave, _feed,
)


class TestMicrophone(unittest.TestCase):
    def test_pcm_device_default_and_peer(self):
        self.assertIn("PROFILE=sco", pcm_device(None))
        self.assertIn("HWCOMPAT=busy", pcm_device("AB:12:34:56:78:CD"))
        with self.assertRaises(ValueError):
            pcm_device("bad-mac")

    def test_detect_hfp_sco_playback_not_a2dp_or_capture(self):
        listing = ("bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp\n"
                   "Android, capture\n"
                   "bluealsa:SRV=org.bluealsa,DEV=AA:BB:CC:DD:EE:FF,PROFILE=sco\n"
                   "Android SCO (CVSD): S16_LE 1 channels 8000 Hz, playback\n")
        self.assertTrue(has_sco_playback(listing))
        self.assertTrue(has_sco_playback(listing, "AA:BB:CC:DD:EE:FF"))
        self.assertFalse(has_sco_playback(listing, "00:00:00:00:00:01"))
        self.assertFalse(has_sco_playback("bluealsa:PROFILE=sco\nSCO capture\n"))

    def test_tone_is_20ms_mono_signed_pcm(self):
        data = next(pcm_tone(sample_rate=8000, frequency=440, gain=0.25))
        self.assertEqual(len(data), 320)
        samples = struct.unpack("<160h", data)
        self.assertEqual(samples[0], 0)
        self.assertGreater(max(samples), 7000)
        self.assertLess(min(samples), -7000)

    def test_silence(self):
        self.assertEqual(next(pcm_silence(sample_rate=16000)), bytes(640))

    def test_wave_generation_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "tone.wav"
            generate_sample(output, seconds=0.2, sample_rate=8000, frequency=440, gain=0.2)
            validate_wave(output, 8000)
            with wave.open(str(output), "rb") as reader:
                self.assertEqual(reader.getnframes(), 1600)
                self.assertEqual(reader.getnchannels(), 1)
            with self.assertRaises(ValueError):
                validate_wave(output, 16000)

    def test_bluealsa_commands_include_live_mic_worker(self):
        config = Config(backend="bluealsa", mode="a2dp", microphone=True, mic_source="tone")
        labels = dict(commands(config))
        self.assertIn("hfp-hf", " ".join(labels["daemon"]))
        self.assertIn("microphone", labels)
        self.assertIn("hearing_emulator.microphone", labels["microphone"])
        self.assertIn("--source", labels["microphone"])
        config = Config(mode="a2dp", microphone=False)
        self.assertNotIn("microphone", dict(commands(config)))

    def test_audio_worker_feeds_pcm_into_a_subprocess(self):
        # `cat` stands in for the process connected to the SCO playback PCM.
        # This checks real bytes cross a pipe without requiring Bluetooth.
        fake_playback = subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        samples = [next(pcm_tone(sample_rate=8000, frequency=440, gain=0.2)) for _ in range(3)]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(microphone, "STATUS_FILE", Path(tmp) / "status.json"):
            _feed(iter(samples), fake_playback)
        recorded, _ = fake_playback.communicate(timeout=3)
        self.assertEqual(recorded, b"".join(samples))

    def test_feed_does_not_probe_while_audio_flows(self):
        # Probing spawns bluealsa-aplay; doing it mid-stream would underrun aplay.
        fake_playback = subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        chunk = next(pcm_tone(sample_rate=8000, frequency=440, gain=0.2))
        clock = iter(range(0, 1000, 3))  # every monotonic() call advances 3 "seconds"
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(microphone, "STATUS_FILE", Path(tmp) / "status.json"), \
                patch.object(microphone.time, "monotonic", lambda: next(clock)), \
                patch.object(microphone, "sco_available") as probe:
            _feed(iter([chunk] * 5), fake_playback, peer="AA:BB:CC:DD:EE:FF")
        fake_playback.communicate(timeout=3)
        probe.assert_not_called()

    def test_validation_does_not_claim_bumble_hfp(self):
        Config(mode="a2dp", backend="bluealsa", microphone=True, mic_source="alsa").validate()
        with self.assertRaisesRegex(ValueError, "bumble backend"):
            Config(mode="a2dp", backend="bumble", microphone=True).validate()
        with self.assertRaises(ValueError):
            Config(mode="asha", microphone=True).validate()
        with self.assertRaises(ValueError):
            Config(mode="a2dp", microphone=True, mic_source="wav").validate()


if __name__ == "__main__":
    unittest.main()
