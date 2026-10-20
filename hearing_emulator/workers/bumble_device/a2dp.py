"""A2DP sink: the media route a phone uses for a headset or speaker (and an iPhone without MFi)."""
from __future__ import annotations

import functools
import logging
from pathlib import Path
import time
from typing import BinaryIO

from bumble import a2dp, avdtp, codecs
from bumble.device import Device
from bumble.rtp import MediaPacket

from .media import Player, recording_path
from .report import Reporter

SDP_HANDLE = 0x00010001


def sbc_capabilities() -> avdtp.MediaCodecCapabilities:
    info = a2dp.SbcMediaCodecInformation
    return avdtp.MediaCodecCapabilities(
        media_type=avdtp.MediaType.AUDIO,
        media_codec_type=a2dp.CodecType.SBC,
        media_codec_information=info(
            sampling_frequency=(info.SamplingFrequency.SF_48000 | info.SamplingFrequency.SF_44100
                                | info.SamplingFrequency.SF_32000 | info.SamplingFrequency.SF_16000),
            channel_mode=(info.ChannelMode.MONO | info.ChannelMode.DUAL_CHANNEL
                          | info.ChannelMode.STEREO | info.ChannelMode.JOINT_STEREO),
            block_length=(info.BlockLength.BL_4 | info.BlockLength.BL_8
                          | info.BlockLength.BL_12 | info.BlockLength.BL_16),
            subbands=info.Subbands.S_4 | info.Subbands.S_8,
            allocation_method=info.AllocationMethod.LOUDNESS | info.AllocationMethod.SNR,
            minimum_bitpool_value=2,
            maximum_bitpool_value=53,
        ),
    )


def aac_capabilities() -> avdtp.MediaCodecCapabilities:
    info = a2dp.AacMediaCodecInformation
    return avdtp.MediaCodecCapabilities(
        media_type=avdtp.MediaType.AUDIO,
        media_codec_type=a2dp.CodecType.MPEG_2_4_AAC,
        media_codec_information=info(
            object_type=info.ObjectType.MPEG_2_AAC_LC | info.ObjectType.MPEG_4_AAC_LC,
            sampling_frequency=info.SamplingFrequency.SF_44100 | info.SamplingFrequency.SF_48000,
            channels=info.Channels.MONO | info.Channels.STEREO,
            vbr=1,
            bitrate=256000,
        ),
    )


def _flag_name(info: object, field: str) -> str:
    return getattr(getattr(info, field, None), "name", None) or ""


def describe_codec(configuration: list) -> tuple[str, str]:
    """Codec name ('SBC'/'AAC') and a readable description of the configured stream."""
    for capability in configuration:
        if isinstance(capability, avdtp.MediaCodecCapabilities):
            info = capability.media_codec_information
            name = "AAC" if capability.media_codec_type == a2dp.CodecType.MPEG_2_4_AAC else "SBC"
            rate = _flag_name(info, "sampling_frequency")  # e.g. SF_44100
            rate = f"{int(rate[3:]) / 1000:g} kHz" if rate[3:].isdigit() else "unknown rate"
            mode = _flag_name(info, "channel_mode") or _flag_name(info, "channels")
            return name, f"{name} {rate} {mode.replace('_', ' ').lower()}".strip()
    return "SBC", "unknown codec"


class A2dpStream:
    """One START..SUSPEND/CLOSE stream: counts packets, optionally records/plays."""

    def __init__(self, codec: str, record_dir: Path | None, play: bool) -> None:
        self.codec = codec
        self.packets = 0
        self.started = time.monotonic()
        suffix = ".sbc" if codec == "SBC" else ".aac"
        self.path = recording_path(record_dir, "a2dp", "media", suffix) if record_dir else None
        self.file: BinaryIO | None = self.path.open("wb") if self.path else None
        self.player = Player(["-f", codec.lower()]) if play else None

    @property
    def seconds(self) -> float:
        return time.monotonic() - self.started

    def on_packet(self, packet: MediaPacket) -> None:
        self.packets += 1
        if not (self.file or self.player):
            return
        # SBC: 1-byte media payload header, then frames. AAC: LATM, repacked as ADTS for players.
        audio = packet.payload[1:] if self.codec == "SBC" \
            else codecs.AacAudioRtpPacket.from_bytes(packet.payload).to_adts()
        if self.file:
            self.file.write(audio)
        if self.player:
            self.player.write(audio)

    def close(self) -> None:
        if self.file:
            self.file.close()
        if self.player:
            self.player.close()


class A2dpSink:
    def __init__(self, device: Device, report: Reporter, record_dir: Path | None,
                 play: bool, offer_aac: bool) -> None:
        self.report = report
        self.record_dir = record_dir
        self.play = play
        # AAC first: phones that support it (iPhones) prefer it; SBC is mandatory for all.
        self.capabilities = ([aac_capabilities()] if offer_aac else []) + [sbc_capabilities()]
        self.codec = ("SBC", "unknown codec")
        self.stream: A2dpStream | None = None
        device.sdp_service_records[SDP_HANDLE] = a2dp.make_audio_sink_service_sdp_records(SDP_HANDLE)
        self.listener = avdtp.Listener.for_device(device)
        self.listener.on(self.listener.EVENT_CONNECTION, self.attach)

    def attach(self, server: avdtp.Protocol) -> None:
        """Expose the sink endpoints on an AVDTP session (opened by the phone or by `connect`)."""
        peer = server.l2cap_channel.connection.peer_address.to_string(False)
        self.report("a2dp", "connected", f"media channel open with phone {peer}")
        for capabilities in self.capabilities:
            sink = server.add_sink(capabilities)
            sink.on(sink.EVENT_CONFIGURATION, functools.partial(self._on_configuration, sink))
            sink.on(sink.EVENT_START, self._on_start)
            sink.on(sink.EVENT_SUSPEND, functools.partial(self._on_end, "suspended", "paused by the phone"))
            sink.on(sink.EVENT_CLOSE, functools.partial(self._on_end, "connected", "stream closed"))
            sink.on(sink.EVENT_ABORT, functools.partial(self._on_end, "connected", "stream aborted"))
            sink.on(sink.EVENT_RTP_PACKET, self._on_rtp_packet)

    def progress(self) -> None:
        if self.stream:
            self.report("a2dp", "streaming", self._summary())

    def close(self) -> None:
        self._end_stream()

    def _summary(self) -> str:
        assert self.stream is not None
        return f"{self.codec[1]}, {self.stream.seconds:.1f} s, {self.stream.packets} packets"

    def _end_stream(self) -> str:
        if not self.stream:
            return ""
        summary = self._summary() + (f", recorded {self.stream.path}" if self.stream.path else "")
        self.stream.close()
        self.stream = None
        return summary

    def _on_configuration(self, sink: avdtp.LocalSink) -> None:
        self.codec = describe_codec(sink.configuration)
        self.report("a2dp", "configured", f"phone selected {self.codec[1]}")

    def _on_start(self) -> None:
        self._end_stream()
        self.stream = A2dpStream(self.codec[0], self.record_dir, self.play)
        self.report("a2dp", "streaming", f"START {self.codec[1]}")

    def _on_end(self, state: str, why: str) -> None:
        summary = self._end_stream()
        self.report("a2dp", state, f"{why}; {summary}" if summary else why)

    def _on_rtp_packet(self, packet: MediaPacket) -> None:
        try:
            if self.stream:
                self.stream.on_packet(packet)
        except Exception as exc:  # noqa: BLE001 - keep the Bluetooth link alive
            logging.getLogger(__name__).warning("Dropped A2DP media packet: %s", exc)
