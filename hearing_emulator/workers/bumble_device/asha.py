"""ASHA hearing device: the device side of Android's Audio Streaming for Hearing Aids.

Presents an LE hearing device with the configured side, pair membership and
HiSyncId, reports what the phone asks for (start/stop, audio type, volume) and can
record or play the received audio.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
import struct
import wave

from bumble import data_types, decoder, gatt
from bumble.core import AdvertisingData, Appearance, PhysicalTransport
from bumble.device import AdvertisingType, Connection, Device
from bumble.profiles import asha
from bumble.profiles.gap import GenericAccessService

from .media import Player, open_wav, recording_path
from .report import Reporter

SAMPLE_RATE = 16000
FRAME_SECONDS = 0.02  # each ASHA SDU: 1-byte sequence counter + 20 ms of 64 kbit/s G.722
ADVERTISING_INTERVAL_MS = 100  # hearing aids advertise fast; Bumble's default is 1 s
APPEARANCE = data_types.Appearance(Appearance.Category.HEARING_AID,
                                   Appearance.HearingAidSubcategory.COCHLEAR_IMPLANT)


class SaturatingG722Decoder(decoder.G722Decoder):
    """Bumble's decoder omits the ITU reference's final 16-bit saturation and raises
    OverflowError on valid input (even an all-zero frame), which would end the stream."""

    def update_decoded_result(self, xout: int, byte_length: int, byte_array: bytearray) -> int:
        sample = max(-32768, min(32767, xout >> 11))
        byte_array[byte_length:byte_length + 2] = sample.to_bytes(2, "little", signed=True)
        return byte_length + 2


def signed_volume(value: int | None) -> int | None:
    """ASHA volume is an int8 in dB (-128 mute .. 0 max); Bumble keeps the raw byte."""
    return None if value is None else struct.unpack("b", bytes([value & 0xFF]))[0]


class AudioStream:
    """One START..STOP stream: counts frames and sequence gaps, optionally records/plays."""

    def __init__(self, audio_type: str, record_dir: Path | None, play: bool) -> None:
        self.audio_type = audio_type
        self.frames = 0
        self.lost = 0
        self.last_sequence: int | None = None
        self.decoder = SaturatingG722Decoder() if record_dir or play else None
        self.path = recording_path(record_dir, "asha", audio_type, ".wav") if record_dir else None
        self.wav: wave.Wave_write | None = open_wav(self.path, SAMPLE_RATE) if self.path else None
        self.player = Player(["-f", "s16le", "-sample_rate", str(SAMPLE_RATE)]) if play else None

    @property
    def seconds(self) -> float:
        return self.frames * FRAME_SECONDS

    def on_sdu(self, sdu: bytes) -> None:
        if not sdu:
            return
        sequence = sdu[0]
        if self.last_sequence is not None:
            self.lost += (sequence - self.last_sequence - 1) % 256
        self.last_sequence = sequence
        self.frames += 1
        if self.decoder is None:
            return
        pcm = self.decoder.decode_frame(sdu[1:])
        if self.wav:
            self.wav.writeframes(pcm)
        if self.player:
            self.player.write(pcm)

    def close(self) -> None:
        if self.wav:
            self.wav.close()
        if self.player:
            self.player.close()


class AshaEmulator:
    def __init__(self, device: Device, options: argparse.Namespace, report: Reporter) -> None:
        self.device = device
        self.options = options
        self.report = report
        self.stream: AudioStream | None = None
        # Bumble clears service.volume on STOP before emitting it; keep the last value for summaries.
        self.volume: int | None = None
        capability = asha.DeviceCapabilities(0)
        if options.side == "right":
            capability |= asha.DeviceCapabilities.IS_RIGHT
        if options.binaural:
            capability |= asha.DeviceCapabilities.IS_DUAL
        self.service = asha.AshaService(capability=capability, hisyncid=bytes.fromhex(options.sync_id),
                                        device=device, audio_sink=self._on_audio)
        device.add_service(GenericAccessService(options.name, APPEARANCE))
        device.add_service(self.service)
        self.service.on(self.service.EVENT_STARTED, self._on_started)
        self.service.on(self.service.EVENT_STOPPED, self._on_stopped)
        self.service.on(self.service.EVENT_VOLUME_CHANGED, self._on_volume)
        device.on(device.EVENT_CONNECTION, self._on_connection)

    def advertising_data(self) -> bytes:
        # Legacy advertising holds 31 bytes; the name goes in the scan response.
        return bytes(AdvertisingData([
            data_types.Flags(AdvertisingData.Flags.LE_GENERAL_DISCOVERABLE_MODE
                             | AdvertisingData.Flags.BR_EDR_NOT_SUPPORTED),
            data_types.IncompleteListOf16BitServiceUUIDs([gatt.GATT_ASHA_SERVICE]),
            APPEARANCE,
        ])) + self.service.get_advertising_data()

    def scan_response_data(self) -> bytes:
        return bytes(AdvertisingData([data_types.CompleteLocalName(self.options.name)]))

    async def start_advertising(self) -> None:
        await self.device.start_advertising(
            advertising_type=AdvertisingType.UNDIRECTED_CONNECTABLE_SCANNABLE,
            auto_restart=True,
            advertising_data=self.advertising_data(),
            scan_response_data=self.scan_response_data(),
            advertising_interval_min=ADVERTISING_INTERVAL_MS,
            advertising_interval_max=ADVERTISING_INTERVAL_MS,
        )
        pair = f"{'binaural' if self.options.binaural else 'monaural'} {self.options.side}"
        self.report("asha", "advertising", f"'{self.options.name}' ({pair}, HiSyncId {self.options.sync_id}, "
                                           f"address {self.options.address}); pair from Android hearing device settings")

    def progress(self) -> None:
        if self.stream:
            self.report("asha", "streaming", self._stream_summary())

    def close(self) -> None:
        self._end_stream()

    def _volume(self) -> str:
        if self.volume is None:
            return "unknown"
        return "muted" if self.volume == -128 else f"{self.volume} dB"

    def _stream_summary(self) -> str:
        assert self.stream is not None
        return (f"{self.stream.audio_type} audio, volume {self._volume()}, "
                f"{self.stream.seconds:.1f} s received, {self.stream.lost} frames lost")

    def _end_stream(self) -> str:
        if not self.stream:
            return ""
        summary = self._stream_summary()
        if self.stream.path:
            summary += f", recorded {self.stream.path}"
        self.stream.close()
        self.stream = None
        return summary

    def _on_connection(self, connection: Connection) -> None:
        if connection.transport != PhysicalTransport.LE:
            return  # Classic links belong to A2DP/HFP
        peer = connection.peer_address.to_string(False)
        self.report("asha", "connected", f"phone {peer} connected")
        connection.on(connection.EVENT_PAIRING,
                      lambda _keys: self.report("asha", "connected", f"bonded with {peer}"))
        connection.on(connection.EVENT_PAIRING_FAILURE,
                      lambda reason: self.report("asha", "connected", f"pairing with {peer} failed: {reason}"))
        connection.on(connection.EVENT_DISCONNECTION, lambda reason: self._on_disconnection(peer, reason))

    def _on_disconnection(self, peer: object, reason: int) -> None:
        summary = self._end_stream()
        detail = f"phone {peer} disconnected (reason 0x{reason:02X})"
        self.report("asha", "advertising", detail + (f"; stream ended: {summary}" if summary else ""))

    def _on_started(self) -> None:
        self._end_stream()
        audio_type = self.service.audio_type.name if self.service.audio_type is not None else "UNKNOWN"
        self.volume = signed_volume(self.service.volume)
        self.stream = AudioStream(audio_type, self.options.record_dir, self.options.play)
        other = "connected" if self.service.other_state else "not connected"
        self.report("asha", "streaming", f"START {audio_type} audio, volume {self._volume()}, other side {other}")

    def _on_stopped(self) -> None:
        summary = self._end_stream()
        self.report("asha", "connected", f"STOP; {summary}" if summary else "STOP")

    def _on_volume(self) -> None:
        self.volume = signed_volume(self.service.volume)
        self.report("asha", "streaming" if self.stream else "connected", f"volume set to {self._volume()}")

    def _on_audio(self, sdu: bytes) -> None:
        # An exception here would propagate into Bumble's L2CAP reassembly and break the channel.
        try:
            if self.stream:
                self.stream.on_sdu(sdu)
        except Exception as exc:  # noqa: BLE001 - keep the Bluetooth link alive
            logging.getLogger(__name__).warning("Dropped ASHA audio frame: %s", exc)
