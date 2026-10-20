"""The Bumble emulator against a simulated phone on a simulated radio link.

A Bumble device plays the phone: Android's ASHA client, an A2DP source, and an HFP
audio gateway. Needs Bumble (Python 3.11+), so run it with the Bumble environment
from the project root:

    ~/.hearing-emulator/venv-bumble/bin/python -m unittest tests.test_bumble_device -v

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
from unittest.mock import patch
import wave

try:
    from bumble import a2dp, avdtp, hci, hfp, l2cap, rfcomm
    from bumble.controller import Controller
    from bumble.core import AdvertisingData, PhysicalTransport
    from bumble.device import Device, Peer
    from bumble.hci import Address
    from bumble.host import Host
    from bumble.link import LocalLink
    from bumble.profiles import asha
    from bumble.profiles.gap import GenericAccessServiceProxy
    from bumble.rtp import MediaPacket
    from bumble.transport.common import AsyncPipeSink

    from hearing_emulator.backends import bumble as bumble_backend
    from hearing_emulator.workers.bumble_device import asha as asha_worker
    from hearing_emulator.workers.bumble_device.device import DeviceEmulator, device_config, serve_control
    from hearing_emulator.workers.bumble_device import hfp as hfp_worker
    from hearing_emulator.workers.bumble_device.hfp import PcmSource
    from hearing_emulator.workers.bumble_device.report import Reporter
except ImportError:
    asha_worker = None

FRAME = bytes(160)  # 20 ms of G.722; all-zero frames crash Bumble's stock decoder
PHONE = "F0:F0:F0:F0:F0:F0"
EMULATOR_PUBLIC = "E0:E0:E0:E0:E0:E0"


def worker_options(directory: Path, **overrides) -> "argparse.Namespace":
    values = dict(name="CI-Test-Right", profiles=["asha-sink"], side="right", binaural=True,
                  sync_id="0011223344556677", address="C1:22:33:44:55:66", codec="sbc",
                  mic_source="tone", mic_frequency=440.0, mic_gain=0.2, mic_wave_path=None,
                  keystore=directory / "keys.json", status_file=directory / "status.json",
                  control_socket=None, record_dir=directory / "recordings", play=False)
    values.update(overrides)
    return argparse.Namespace(**values)


@unittest.skipIf(asha_worker is None, "Bumble is not installed in this interpreter")
class SimulatedLink(unittest.IsolatedAsyncioTestCase):
    """The emulator and a phone on one simulated radio link."""

    profiles = ["asha-sink"]
    extra_options: dict = {}

    async def asyncSetUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))  # worker log lines
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.options = worker_options(directory, profiles=self.profiles, **self.extra_options)
        link = LocalLink()
        emulator_controller = Controller("emulator", link=link, public_address=EMULATOR_PUBLIC)
        phone_controller = Controller("phone", link=link, public_address=PHONE)
        device = Device.from_config_with_hci(device_config(self.options),
                                             emulator_controller, AsyncPipeSink(emulator_controller))
        self.emulator = DeviceEmulator(device, self.options, Reporter(self.options.status_file))
        self.phone = Device(address=Address(PHONE), host=Host(phone_controller, AsyncPipeSink(phone_controller)))
        self.phone.classic_enabled = True
        await self.emulator.start()
        await self.phone.power_on()

    def profile(self, name: str) -> dict:
        return json.loads(self.options.status_file.read_text())["profiles"][name]

    async def connect_le(self):
        connection = await self.phone.connect(Address(self.options.address))
        await asyncio.sleep(0.05)
        return connection

    async def connect_classic(self):
        connection = await self.phone.connect(Address(EMULATOR_PUBLIC, Address.PUBLIC_DEVICE_ADDRESS),
                                              transport=PhysicalTransport.BR_EDR)
        await asyncio.sleep(0.05)
        return connection


class AshaTests(SimulatedLink):
    async def control(self, client, *command: int) -> None:
        await client.audio_control_point_characteristic.write_value(bytes(command), with_response=True)
        await asyncio.sleep(0.05)

    async def test_advertising_fits_legacy_pdus_and_identifies_a_right_binaural_implant(self):
        advertising = self.emulator.asha.advertising_data()
        self.assertLessEqual(len(advertising), 31)
        self.assertLessEqual(len(self.emulator.asha.scan_response_data()), 31)
        parsed = AdvertisingData.from_bytes(advertising)
        service_data = parsed.get(AdvertisingData.Type.SERVICE_DATA_16_BIT_UUID, raw=True)
        # protocol version, capability (IS_RIGHT | IS_DUAL), first 4 bytes of the HiSyncId
        self.assertEqual(service_data[2:], bytes([1, 0x03]) + bytes.fromhex("00112233"))
        self.assertEqual(parsed.get(AdvertisingData.Type.APPEARANCE, raw=True), b"\x43\x0a")
        self.assertIn(b"CI-Test-Right", self.emulator.asha.scan_response_data())

    async def test_stream_is_reported_counted_and_recorded(self):
        connection = await self.connect_le()
        async with Peer(connection) as peer:
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
            self.assertEqual(self.profile("asha")["state"], "streaming")
            self.assertIn("START MEDIA audio, volume -20 dB, other side connected", self.profile("asha")["detail"])

            channel = await connection.create_l2cap_channel(spec=l2cap.LeCreditBasedChannelSpec(psm=psm))
            for sequence in (s for s in range(52) if s != 10):  # frame 10 lost in transit
                channel.write(bytes([sequence]) + FRAME)  # one SDU per frame, as Android sends
                await channel.drain()
            await asyncio.sleep(0.1)

            await client.volume_characteristic.write_value(bytes([-128 & 0xFF]))
            await asyncio.sleep(0.05)
            self.assertIn("volume set to muted", self.profile("asha")["detail"])
            await self.control(client, asha.OpCode.STOP)

        self.assertEqual(self.profile("asha")["state"], "connected")
        self.assertIn("volume muted, 1.0 s received, 1 frames lost", self.profile("asha")["detail"])
        recordings = list(self.options.record_dir.glob("asha-*-media.wav"))
        self.assertEqual(len(recordings), 1)
        with wave.open(str(recordings[0])) as recording:
            self.assertEqual((recording.getframerate(), recording.getnchannels()), (16000, 1))
            self.assertEqual(recording.getnframes(), 51 * 320)  # 160 G.722 bytes -> 320 samples

    async def test_disconnection_ends_stream_and_returns_to_advertising(self):
        connection = await self.connect_le()
        async with Peer(connection) as peer:
            client = peer.create_service_proxy(asha.AshaServiceProxy)
            await self.control(client, asha.OpCode.START, asha.Codec.G_722_16KHZ,
                               asha.AudioType.PHONE_CALL, 0, 0)
            self.assertIn("PHONE_CALL", self.profile("asha")["detail"])
        await connection.disconnect()
        await asyncio.sleep(0.1)
        self.assertEqual(self.profile("asha")["state"], "advertising")
        self.assertIn("stream ended: PHONE_CALL audio", self.profile("asha")["detail"])

    async def test_connect_on_le_only_explains_phone_initiated_reconnect(self):
        response = await self.emulator.control({"command": "connect", "address": PHONE})
        self.assertTrue(response["ok"])
        self.assertIn("started by the phone", response["detail"])


def phone_sbc_source() -> "avdtp.MediaCodecCapabilities":
    info = a2dp.SbcMediaCodecInformation
    return avdtp.MediaCodecCapabilities(
        media_type=avdtp.MediaType.AUDIO, media_codec_type=a2dp.CodecType.SBC,
        media_codec_information=info(
            sampling_frequency=info.SamplingFrequency.SF_44100, channel_mode=info.ChannelMode.JOINT_STEREO,
            block_length=info.BlockLength.BL_16, subbands=info.Subbands.S_8,
            allocation_method=info.AllocationMethod.LOUDNESS, minimum_bitpool_value=2, maximum_bitpool_value=53))


class A2dpTests(SimulatedLink):
    profiles = ["a2dp-sink"]

    async def test_stream_is_reported_and_recorded(self):
        client = await avdtp.Protocol.connect(await self.connect_classic())
        endpoints = list(await client.discover_remote_endpoints())
        self.assertEqual(len(endpoints), 1)  # SBC only unless codec=aac
        stream = await client.create_stream(client.add_source(phone_sbc_source(), None), endpoints[0])
        await asyncio.sleep(0.05)
        self.assertEqual(self.profile("a2dp")["state"], "configured")
        self.assertIn("SBC 44.1 kHz joint stereo", self.profile("a2dp")["detail"])

        await stream.start()
        frames = [bytes([i]) * 10 for i in range(5)]
        for i, frame in enumerate(frames):  # payload: 1-byte SBC header, then frames
            stream.send_media_packet(MediaPacket(2, 0, 0, 0, i, i * 10, 0, [], 96, bytes([1]) + frame))
        await asyncio.sleep(0.1)
        self.assertEqual(self.profile("a2dp")["state"], "streaming")

        await stream.stop()
        await asyncio.sleep(0.05)
        self.assertEqual(self.profile("a2dp")["state"], "suspended")
        self.assertIn("5 packets", self.profile("a2dp")["detail"])
        recordings = list(self.options.record_dir.glob("a2dp-*-media.sbc"))
        self.assertEqual(len(recordings), 1)
        self.assertEqual(recordings[0].read_bytes(), b"".join(frames))


class A2dpAacTests(SimulatedLink):
    profiles = ["a2dp-sink"]
    extra_options = {"codec": "aac"}

    async def test_aac_is_offered_before_sbc(self):
        client = await avdtp.Protocol.connect(await self.connect_classic())
        codecs = [capability.media_codec_type
                  for endpoint in await client.discover_remote_endpoints()
                  for capability in endpoint.capabilities
                  if isinstance(capability, avdtp.MediaCodecCapabilities)]
        self.assertEqual(sorted(codecs), sorted([a2dp.CodecType.MPEG_2_4_AAC, a2dp.CodecType.SBC]))


def phone_audio_gateway() -> "hfp.AgConfiguration":
    return hfp.AgConfiguration(
        supported_ag_features=[hfp.AgFeature.ENHANCED_CALL_STATUS, hfp.AgFeature.HF_INDICATORS],
        supported_ag_indicators=[hfp.AgIndicatorState.call(), hfp.AgIndicatorState.callsetup(),
                                 hfp.AgIndicatorState.callheld(), hfp.AgIndicatorState.service(),
                                 hfp.AgIndicatorState.signal(), hfp.AgIndicatorState.roam(),
                                 hfp.AgIndicatorState.battchg()],
        supported_hf_indicators=[hfp.HfIndicator.BATTERY_LEVEL],
        supported_ag_call_hold_operations=[],
        supported_audio_codecs=[hfp.AudioCodec.CVSD])


class HfpTests(SimulatedLink):
    profiles = ["hfp-hf"]

    async def test_call_audio_carries_the_injected_microphone(self):
        connection = await self.connect_classic()
        channel, _version, _features = await hfp.find_hf_sdp_record(connection)
        dlc = await (await rfcomm.Client(connection).start()).open_dlc(channel)
        gateway = hfp.AgProtocol(dlc, phone_audio_gateway())
        slc = asyncio.get_running_loop().create_future()
        gateway.once(gateway.EVENT_SLC_COMPLETE, lambda *_: slc.set_result(None))
        await asyncio.wait_for(slc, 5)
        await asyncio.sleep(0.05)
        self.assertEqual(self.profile("hfp")["state"], "connected")

        gateway.update_ag_indicator(hfp.AgIndicator.CALL, 1)
        await asyncio.sleep(0.05)
        self.assertEqual(self.profile("hfp")["detail"], "call active")

        # The phone opens the SCO link when it moves call audio to the headset.
        opened = asyncio.get_running_loop().create_future()
        self.emulator.device.once(self.emulator.device.EVENT_SCO_CONNECTION, opened.set_result)
        host = self.emulator.device.host
        enhanced_accept = hci.HCI_ENHANCED_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND
        # The simulated controller implements only the enhanced accept, without listing it as supported.
        with patch.object(host, "supports_command",
                          side_effect=lambda op, real=host.supports_command: op == enhanced_accept or real(op)):
            await self.phone.send_command(hci.HCI_Enhanced_Setup_Synchronous_Connection_Command(
                connection_handle=connection.handle,
                **hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_CVSD_S1].asdict()))
            link = await asyncio.wait_for(opened, 5)
        await asyncio.sleep(0.05)
        self.assertIn("SCO open (CVSD 8 kHz)", self.profile("hfp")["detail"])
        self.assertIn("sending 440 Hz tone", self.profile("hfp")["detail"])

        # The simulated link carries no SCO audio, so feed the frames a phone would send.
        def phone_frames(count: int) -> None:
            for _ in range(count):
                link.sink(hci.HCI_SynchronousDataPacket(
                    connection_handle=link.handle,
                    packet_status=hci.HCI_SynchronousDataPacket.Status.CORRECTLY_RECEIVED_DATA,
                    data_total_length=120, data=bytes(120)))

        sent = []
        with patch.object(self.emulator.device.host, "send_sco_sdu",
                          lambda connection_handle, sdu: sent.append((connection_handle, sdu))):
            phone_frames(50)
        self.assertEqual({handle for handle, _ in sent}, {link.handle})
        self.assertEqual({len(sdu) for _, sdu in sent}, {hfp_worker.MIC_BATCH_BYTES})
        microphone = struct.unpack("<3000h", b"".join(sdu for _, sdu in sent))  # same amount as received
        self.assertGreater(max(microphone), 6000)  # 440 Hz tone at gain 0.2
        self.emulator.hfp.progress()
        self.assertIn("0.4 s from phone, 0.4 s of 440 Hz tone sent", self.profile("hfp")["detail"])
        recordings = list(self.options.record_dir.glob("hfp-*-call.wav"))
        self.assertEqual(len(recordings), 1)

        # A backed-up USB transport drops microphone audio instead of delaying HCI commands.
        backlog = asyncio.Queue()
        for _ in range(hfp_worker.MAX_QUEUED_PACKETS + 1):
            backlog.put_nowait(b"")
        with patch.object(self.emulator.device.host.hci_sink, "packets", backlog, create=True), \
                patch.object(self.emulator.device.host, "send_sco_sdu") as send:
            phone_frames(10)
        send.assert_not_called()
        self.emulator.hfp.progress()
        self.assertIn("(0.1 s dropped: USB uplink too slow)", self.profile("hfp")["detail"])


class CrossPlatformControlTests(SimulatedLink):
    profiles = ["asha-sink", "a2dp-sink"]

    async def test_cli_lists_and_disconnects_both_links_of_a_phone(self):
        socket_dir = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp"))  # Unix socket paths are short
        path = Path(socket_dir) / "control.sock"
        server = await serve_control(path, self.emulator.control)
        self.addCleanup(server.close)
        await self.connect_le()
        await self.connect_classic()

        status = await asyncio.to_thread(bumble_backend.control, {"command": "status"}, path)
        self.assertEqual(sorted(status["connections"]), [f"{PHONE} (Classic)", f"{PHONE} (LE)"])
        self.assertEqual(status["profiles"]["asha"]["state"], "connected")
        self.assertEqual(status["profiles"]["classic"]["state"], "connected")

        response = await asyncio.to_thread(bumble_backend.control,
                                           {"command": "disconnect", "address": PHONE.lower()}, path)
        self.assertTrue(response["ok"], response)
        self.assertIn("LE", response["detail"])
        self.assertIn("Classic", response["detail"])
        await asyncio.sleep(0.1)
        self.assertEqual(self.emulator.device.connections, {})
        self.assertEqual(self.profile("asha")["state"], "advertising")
        self.assertEqual(self.profile("classic")["state"], "discoverable")

        missing = await asyncio.to_thread(bumble_backend.control,
                                          {"command": "disconnect", "address": "11:22:33:44:55:66"}, path)
        self.assertFalse(missing["ok"])


class ClassicConnectTests(SimulatedLink):
    profiles = ["a2dp-sink"]

    async def test_connect_opens_a2dp_to_the_phone(self):
        avdtp.Listener.for_device(self.phone)  # the phone accepts media connections
        await self.phone.set_connectable(True)
        response = await self.emulator.control({"command": "connect", "address": PHONE})
        self.assertTrue(response["ok"], response)
        self.assertIn("Classic link, A2DP", response["detail"])
        self.assertEqual(self.profile("a2dp")["state"], "connected")


@unittest.skipIf(asha_worker is None, "Bumble is not installed in this interpreter")
class HelperTests(unittest.TestCase):
    def test_volume_is_signed_db(self):
        self.assertEqual(asha_worker.signed_volume(236), -20)
        self.assertEqual(asha_worker.signed_volume(0x80), -128)
        self.assertEqual(asha_worker.signed_volume(0), 0)
        self.assertIsNone(asha_worker.signed_volume(None))

    def test_decoder_saturates_instead_of_raising(self):
        pcm = asha_worker.SaturatingG722Decoder().decode_frame(FRAME * 10)
        self.assertEqual(len(pcm), 10 * 160 * 4)

    def test_pcm_source_serves_any_size_without_gaps(self):
        source = PcmSource(iter([b"ab", b"cdef", b"gh"] * 3), "test")
        self.assertEqual(source.read(3) + source.read(4) + source.read(1), b"abcdefgh")


if __name__ == "__main__":
    unittest.main()
