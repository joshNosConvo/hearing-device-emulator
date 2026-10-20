"""HFP hands-free: the headset role, with call audio over SCO and an injected microphone.

Only CVSD (narrowband, 8 kHz) is offered: the controller converts CVSD on the air to
16-bit PCM over HCI. Wideband mSBC would need a software codec, which Bumble lacks.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
import logging
from pathlib import Path
import time
import wave

from bumble import hci, hfp, rfcomm
from bumble.device import Connection, Device, ScoLink

from .media import open_wav, recording_path
from .report import Reporter

SDP_HANDLE = 0x00010002
SAMPLE_RATE = 8000
CALL_AUDIO = "call audio"  # state while an SCO link carries call audio
CALL_STATES = {
    hfp.AgIndicator.CALL: {0: "no active call", 1: "call active"},
    hfp.AgIndicator.CALL_SETUP: {0: "call setup ended", 1: "incoming call", 2: "outgoing call dialing",
                                 3: "outgoing call ringing"},
    hfp.AgIndicator.CALL_HELD: {0: "no held call", 1: "call on hold, another active", 2: "call on hold"},
}


def configuration() -> hfp.HfConfiguration:
    return hfp.HfConfiguration(
        supported_hf_features=[
            hfp.HfFeature.ENHANCED_CALL_STATUS,
            hfp.HfFeature.REMOTE_VOLUME_CONTROL,
            hfp.HfFeature.CLI_PRESENTATION_CAPABILITY,
            hfp.HfFeature.ESCO_S4_SETTINGS_SUPPORTED,
        ],
        supported_hf_indicators=[hfp.HfIndicator.BATTERY_LEVEL],
        supported_audio_codecs=[hfp.AudioCodec.CVSD],
    )


class PcmSource:
    """Serves 16-bit mono PCM in whatever sizes the SCO link asks for."""

    def __init__(self, chunks: Iterator[bytes], label: str) -> None:
        self.chunks = chunks
        self.label = label
        self.buffer = bytearray()

    def read(self, size: int) -> bytes:
        while len(self.buffer) < size:
            self.buffer += next(self.chunks)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data


class CallAudio:
    """One SCO link: counts audio both ways, optionally records what the phone sends."""

    def __init__(self, record_dir: Path | None) -> None:
        self.received = 0  # samples from the phone
        self.sent = 0  # microphone samples to the phone
        self.path = recording_path(record_dir, "hfp", "call", ".wav") if record_dir else None
        self.wav: wave.Wave_write | None = open_wav(self.path, SAMPLE_RATE) if self.path else None

    def close(self) -> None:
        if self.wav:
            self.wav.close()


class HandsFree:
    def __init__(self, device: Device, report: Reporter, microphone: PcmSource | None,
                 record_dir: Path | None) -> None:
        self.device = device
        self.report = report
        self.microphone = microphone
        self.record_dir = record_dir
        self.configuration = configuration()
        self.call: CallAudio | None = None
        self.state = "waiting"
        channel = rfcomm.Server(device).listen(self.attach)
        device.sdp_service_records[SDP_HANDLE] = hfp.make_hf_sdp_records(SDP_HANDLE, channel, self.configuration)
        device.on(device.EVENT_SCO_REQUEST, self._on_sco_request)
        device.on(device.EVENT_SCO_CONNECTION, self._on_sco_connection)

    def attach(self, dlc: rfcomm.DLC) -> None:
        """Run the hands-free side on an RFCOMM channel (opened by the phone or by `connect`)."""
        protocol = hfp.HfProtocol(dlc, self.configuration)
        protocol.on(protocol.EVENT_AG_INDICATOR, self._on_indicator)
        protocol.on(protocol.EVENT_RING, lambda *_: self._set("ringing", "phone is ringing the headset"))
        protocol.on(protocol.EVENT_SPEAKER_VOLUME,
                    lambda level: self._set(self.state, f"speaker volume {level}/15"))
        protocol.on(protocol.EVENT_MICROPHONE_VOLUME,
                    lambda level: self._set(self.state, f"microphone volume {level}/15"))
        asyncio.get_running_loop().create_task(self._run(protocol, dlc.multiplexer.l2cap_channel.connection))

    def progress(self) -> None:
        if self.call:
            self._set(CALL_AUDIO, self._call_summary())

    def close(self) -> None:
        if self.call:
            self.call.close()
            self.call = None

    def _set(self, state: str, detail: str) -> None:
        self.state = state
        self.report("hfp", state, detail)

    async def _run(self, protocol: hfp.HfProtocol, connection: Connection) -> None:
        try:
            await protocol.initiate_slc()
        except Exception as exc:  # noqa: BLE001 - report and leave the link to the phone
            self._set("failed", f"service level connection with {connection.peer_address.to_string(False)} failed: {exc}")
            return
        self._set("connected", f"service level connection with phone {connection.peer_address.to_string(False)}; "
                               "calls can route to this headset")
        await protocol.run()

    def _on_indicator(self, indicator: hfp.AgIndicatorState) -> None:
        states = CALL_STATES.get(indicator.indicator)
        if states:  # signal, battery and roaming changes are noise for routing tests
            detail = states.get(indicator.current_status, f"{indicator.indicator.value}={indicator.current_status}")
            self._set(CALL_AUDIO if self.call else "connected", detail)

    def _on_sco_request(self, connection: Connection, link_type: int) -> None:
        if link_type == hci.HCI_Connection_Complete_Event.LinkType.SCO:
            parameters = hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.SCO_CVSD_D1]
        else:
            parameters = hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_CVSD_S4]
        if self.device.host.supports_command(hci.HCI_ENHANCED_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND):
            command = hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command(
                bd_addr=connection.peer_address, **parameters.asdict())
        else:
            command = hci.HCI_Accept_Synchronous_Connection_Request_Command(
                bd_addr=connection.peer_address,
                transmit_bandwidth=parameters.transmit_bandwidth,
                receive_bandwidth=parameters.receive_bandwidth,
                max_latency=parameters.max_latency,
                voice_setting=int(hci.VoiceSetting(
                    input_sample_size=hci.VoiceSetting.InputSampleSize.SIZE_16_BITS,
                    input_data_format=hci.VoiceSetting.InputDataFormat.TWOS_COMPLEMENT)),
                retransmission_effort=parameters.retransmission_effort,
                packet_type=parameters.packet_type,
            )
        connection.cancel_on_disconnection(self.device.send_async_command(command))

    def _on_sco_connection(self, link: ScoLink) -> None:
        if link.air_mode == hci.CodecID.TRANSPARENT:
            self._set(CALL_AUDIO, "SCO opened in transparent (mSBC) mode; the injected microphone needs CVSD")
            return
        self.close()
        self.call = CallAudio(self.record_dir)
        link.sink = self._on_sco_packet
        link.on(link.EVENT_DISCONNECTION, self._on_sco_disconnection)
        source = f"sending {self.microphone.label}" if self.microphone else "microphone off"
        self._set(CALL_AUDIO, f"SCO open (CVSD 8 kHz) with {link.acl_connection.peer_address.to_string(False)}; {source}")

    def _on_sco_disconnection(self, _reason: int) -> None:
        summary = self._call_summary() if self.call else ""
        self.close()
        self._set("connected", f"call audio closed; {summary}" if summary else "call audio closed")

    def _call_summary(self) -> str:
        assert self.call is not None
        summary = f"{self.call.received / SAMPLE_RATE:.1f} s from phone"
        if self.microphone:
            summary += f", {self.call.sent / SAMPLE_RATE:.1f} s of {self.microphone.label} sent"
        if self.call.received == 0:
            summary += " (no SCO audio reaching the computer yet: the adapter must carry SCO over USB)"
        if self.call.path:
            summary += f", recording {self.call.path}"
        return summary

    def _on_sco_packet(self, packet: hci.HCI_SynchronousDataPacket) -> None:
        # The phone sends SCO frames at the link's fixed rate; answering each with the same
        # amount of microphone audio keeps the uplink paced without a timer.
        try:
            if not self.call:
                return
            samples = len(packet.data) // 2
            self.call.received += samples
            if self.call.wav and packet.packet_status == hci.HCI_SynchronousDataPacket.Status.CORRECTLY_RECEIVED_DATA:
                self.call.wav.writeframes(packet.data)
            if self.microphone:
                self.device.host.send_sco_sdu(connection_handle=packet.connection_handle,
                                              sdu=self.microphone.read(len(packet.data)))
                self.call.sent += samples
        except Exception as exc:  # noqa: BLE001 - keep the call link alive
            logging.getLogger(__name__).warning("Dropped SCO frame: %s", exc)
