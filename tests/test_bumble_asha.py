"""End-to-end ASHA flow against the Bumble emulator on a simulated radio link.

A Bumble central plays Android's part: it reads the ASHA properties, starts a
stream, sends G.722 frames over the LE credit-based channel, changes volume and
stops. Needs Bumble (Python 3.11+), so run it with the Bumble environment from
the project root:

    ~/.hearing-emulator/venv-bumble/bin/python -m unittest tests.test_bumble_asha -v

Under an interpreter without Bumble (the CLI's own) the tests are skipped.
"""
import argparse
import asyncio
import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import wave

try:
    from bumble import l2cap
    from bumble.controller import Controller
    from bumble.core import AdvertisingData
    from bumble.device import Device, Peer
    from bumble.hci import Address
    from bumble.host import Host
    from bumble.link import LocalLink
    from bumble.profiles import asha
    from bumble.profiles.gap import GenericAccessServiceProxy
    from bumble.transport.common import AsyncPipeSink

    from hearing_emulator.workers import bumble_asha
except ImportError:
    bumble_asha = None

FRAME = bytes(160)  # 20 ms of G.722; all-zero frames crash Bumble's stock decoder


@unittest.skipIf(bumble_asha is None, "Bumble is not installed in this interpreter")
class AshaEmulatorFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        self.options = argparse.Namespace(
            name="CI-Test-Right", side="right", binaural=True, sync_id="0011223344556677",
            address="C1:22:33:44:55:66", keystore=directory / "keys.json",
            status_file=directory / "status.json", record_dir=directory / "recordings", play=False)
        link = LocalLink()
        emulator_controller = Controller("emulator", link=link)
        phone_controller = Controller("phone", link=link, public_address="F0:F0:F0:F0:F0:F0")
        device = Device.from_config_with_hci(bumble_asha.device_config(self.options),
                                             emulator_controller, AsyncPipeSink(emulator_controller))
        self.emulator = bumble_asha.AshaEmulator(device, self.options,
                                                 bumble_asha.Reporter(self.options.status_file))
        self.phone = Device(address=Address("F0:F0:F0:F0:F0:F0"),
                            host=Host(phone_controller, AsyncPipeSink(phone_controller)))
        with contextlib.redirect_stdout(io.StringIO()):
            await self.emulator.start()
            await self.phone.power_on()
            self.connection = await self.phone.connect(Address(self.options.address))
        await asyncio.sleep(0.05)

    def status(self) -> dict:
        return json.loads(self.options.status_file.read_text())

    async def control(self, client, *command: int) -> None:
        await client.audio_control_point_characteristic.write_value(bytes(command), with_response=True)
        await asyncio.sleep(0.05)

    def test_advertising_fits_legacy_pdus_and_identifies_a_right_binaural_implant(self):
        advertising = self.emulator.advertising_data()
        self.assertLessEqual(len(advertising), 31)
        self.assertLessEqual(len(self.emulator.scan_response_data()), 31)
        parsed = AdvertisingData.from_bytes(advertising)
        service_data = parsed.get(AdvertisingData.Type.SERVICE_DATA_16_BIT_UUID, raw=True)
        # protocol version, capability (IS_RIGHT | IS_DUAL), first 4 bytes of the HiSyncId
        self.assertEqual(service_data[2:], bytes([1, 0x03]) + bytes.fromhex("00112233"))
        self.assertEqual(parsed.get(AdvertisingData.Type.APPEARANCE, raw=True), b"\x43\x0a")
        self.assertIn(b"CI-Test-Right", self.emulator.scan_response_data())

    async def test_stream_is_reported_counted_and_recorded(self):
        with contextlib.redirect_stdout(io.StringIO()):
            async with Peer(self.connection) as peer:
                gap = peer.create_service_proxy(GenericAccessServiceProxy)
                self.assertEqual(int(await gap.appearance.read_value()), 0x0A43)
                client = peer.create_service_proxy(asha.AshaServiceProxy)
                properties = await client.read_only_properties_characteristic.read_value()
                _, capability, hisyncid, *_ = struct.unpack("<BB8sBH2sH", properties)
                self.assertEqual(capability, 0x03)
                self.assertEqual(hisyncid.hex(), "0011223344556677")
                psm = struct.unpack("<H", await client.psm_characteristic.read_value())[0]

                await self.control(client, asha.OpCode.START, asha.Codec.G_722_16KHZ,
                                   asha.AudioType.MEDIA, -20 & 0xFF, 1)
                self.assertEqual(self.status()["state"], "streaming")
                self.assertIn("START MEDIA audio, volume -20 dB, other side connected",
                              self.status()["detail"])

                channel = await self.connection.create_l2cap_channel(
                    spec=l2cap.LeCreditBasedChannelSpec(psm=psm))
                for sequence in (s for s in range(52) if s != 10):  # frame 10 lost in transit
                    channel.write(bytes([sequence]) + FRAME)  # one SDU per frame, as Android sends
                    await channel.drain()
                await asyncio.sleep(0.1)

                await client.volume_characteristic.write_value(bytes([-128 & 0xFF]))
                await asyncio.sleep(0.05)
                self.assertIn("volume set to muted", self.status()["detail"])
                await self.control(client, asha.OpCode.STOP)

        status = self.status()
        self.assertEqual(status["state"], "connected")
        self.assertIn("volume muted, 1.0 s received, 1 frames lost", status["detail"])
        recordings = list(self.options.record_dir.glob("asha-*-media.wav"))
        self.assertEqual(len(recordings), 1)
        with wave.open(str(recordings[0])) as recording:
            self.assertEqual((recording.getframerate(), recording.getnchannels()), (16000, 1))
            self.assertEqual(recording.getnframes(), 51 * 320)  # 160 G.722 bytes -> 320 samples

    async def test_disconnection_ends_stream_and_returns_to_advertising(self):
        with contextlib.redirect_stdout(io.StringIO()):
            async with Peer(self.connection) as peer:
                client = peer.create_service_proxy(asha.AshaServiceProxy)
                await self.control(client, asha.OpCode.START, asha.Codec.G_722_16KHZ,
                                   asha.AudioType.PHONE_CALL, 0, 0)
                self.assertIn("PHONE_CALL", self.status()["detail"])
            await self.connection.disconnect()
            await asyncio.sleep(0.1)
        status = self.status()
        self.assertEqual(status["state"], "advertising")
        self.assertIn("disconnected", status["detail"])
        self.assertIn("stream ended: PHONE_CALL audio", status["detail"])


@unittest.skipIf(bumble_asha is None, "Bumble is not installed in this interpreter")
class AshaHelpersTests(unittest.TestCase):
    def test_volume_is_signed_db(self):
        self.assertEqual(bumble_asha.signed_volume(236), -20)
        self.assertEqual(bumble_asha.signed_volume(0x80), -128)
        self.assertEqual(bumble_asha.signed_volume(0), 0)
        self.assertIsNone(bumble_asha.signed_volume(None))

    def test_decoder_saturates_instead_of_raising(self):
        pcm = bumble_asha.SaturatingG722Decoder().decode_frame(FRAME * 10)
        self.assertEqual(len(pcm), 10 * 160 * 4)


if __name__ == "__main__":
    unittest.main()
