"""Narrow protocol helpers used by the PLAF203 bootstrap listener.

This module intentionally implements only the OEM provisioning and MQTT packet
surface required for bootstrap.  It is not a general MQTT implementation.
"""

from __future__ import annotations

import dataclasses
import json
import struct
from typing import Iterable


PROVISIONING_MAGIC = b"DL"
PROVISIONING_COMMAND_SET_MQTT_SERVER = 0x02


class ProtocolError(ValueError):
    """Raised when an inbound packet is malformed or unsupported."""


def build_set_mqtt_server_frame(
    host: str, port: int, member_id: str = "1", sequence: int = 1
) -> bytes:
    """Build the normal, checksum-free OEM provisioning command 0x02 frame."""

    if not host or ":" in host or "\x00" in host:
        raise ValueError("provisioning host must be a non-empty host without ':'")
    # SetMqttServerFromProvisioning copies strlen(endpoint)-5 bytes as the
    # host.  The OEM implementation therefore only parses :NNNN correctly.
    if not 1000 <= port <= 9999:
        raise ValueError("provisioning port must contain exactly four decimal digits")
    if not member_id.isdecimal() or not member_id:
        raise ValueError("member_id must contain decimal digits")
    if not 0 <= sequence <= 255:
        raise ValueError("sequence must fit in one byte")

    endpoint = f"{host}:{port}".encode("ascii")
    member = member_id.encode("ascii")
    if len(endpoint) > 99:
        raise ValueError("broker endpoint exceeds the firmware's 99-byte safe limit")
    if len(member) > 15:
        raise ValueError("member_id is too long")
    payload_length = len(endpoint) + len(member) + 2
    if payload_length > 255:
        raise ValueError("provisioning payload is too long")
    return (
        PROVISIONING_MAGIC
        + bytes(
            (
                PROVISIONING_COMMAND_SET_MQTT_SERVER,
                0,  # protocol mode 0: no optional checksum
                sequence,
                payload_length,
                len(endpoint),
            )
        )
        + endpoint
        + bytes((len(member),))
        + member
        + b"\xff"
    )


def encode_remaining_length(length: int) -> bytes:
    if not 0 <= length <= 268_435_455:
        raise ValueError("MQTT remaining length is out of range")
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 0x80
        encoded.append(digit)
        if not length:
            return bytes(encoded)


def decode_remaining_length(data: bytes, offset: int = 1) -> tuple[int, int]:
    value = 0
    multiplier = 1
    for index in range(4):
        if offset + index >= len(data):
            raise ProtocolError("truncated MQTT remaining length")
        digit = data[offset + index]
        value += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            return value, offset + index + 1
        multiplier *= 128
    raise ProtocolError("invalid MQTT remaining length")


def encode_utf8(value: str | bytes) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if len(raw) > 65535:
        raise ValueError("MQTT string is too long")
    return struct.pack("!H", len(raw)) + raw


def _read_u16(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 2 > len(data):
        raise ProtocolError("truncated MQTT uint16")
    return struct.unpack_from("!H", data, offset)[0], offset + 2


def _read_binary(data: bytes, offset: int) -> tuple[bytes, int]:
    length, offset = _read_u16(data, offset)
    end = offset + length
    if end > len(data):
        raise ProtocolError("truncated MQTT binary string")
    return data[offset:end], end


@dataclasses.dataclass(frozen=True)
class MqttConnect:
    client_id: str
    username: str | None
    password: bytes | None
    keep_alive: int
    protocol_level: int


def parse_connect(packet: bytes) -> MqttConnect:
    if not packet or packet[0] >> 4 != 1:
        raise ProtocolError("packet is not MQTT CONNECT")
    remaining, offset = decode_remaining_length(packet)
    if offset + remaining != len(packet):
        raise ProtocolError("MQTT CONNECT length mismatch")
    protocol_name, offset = _read_binary(packet, offset)
    if offset + 4 > len(packet):
        raise ProtocolError("truncated MQTT CONNECT variable header")
    protocol_level = packet[offset]
    flags = packet[offset + 1]
    keep_alive = struct.unpack_from("!H", packet, offset + 2)[0]
    offset += 4
    if protocol_name != b"MQTT" or protocol_level != 4:
        raise ProtocolError("only MQTT 3.1.1 CONNECT is supported")
    if flags & 0x01:
        raise ProtocolError("reserved MQTT CONNECT flag is set")

    client_id_raw, offset = _read_binary(packet, offset)
    if flags & 0x04:
        _, offset = _read_binary(packet, offset)  # will topic
        _, offset = _read_binary(packet, offset)  # will payload
    username_raw = None
    password = None
    if flags & 0x80:
        username_raw, offset = _read_binary(packet, offset)
    if flags & 0x40:
        password, offset = _read_binary(packet, offset)
    if offset != len(packet):
        raise ProtocolError("trailing bytes in MQTT CONNECT")
    try:
        client_id = client_id_raw.decode("utf-8")
        username = username_raw.decode("utf-8") if username_raw is not None else None
    except UnicodeDecodeError as err:
        raise ProtocolError("MQTT identity is not UTF-8") from err
    return MqttConnect(client_id, username, password, keep_alive, protocol_level)


@dataclasses.dataclass(frozen=True)
class MqttSubscribe:
    packet_id: int
    topics: tuple[tuple[str, int], ...]


def parse_subscribe(packet: bytes) -> MqttSubscribe:
    if not packet or packet[0] >> 4 != 8 or packet[0] & 0x0F != 2:
        raise ProtocolError("packet is not a valid MQTT SUBSCRIBE")
    remaining, offset = decode_remaining_length(packet)
    if offset + remaining != len(packet):
        raise ProtocolError("MQTT SUBSCRIBE length mismatch")
    packet_id, offset = _read_u16(packet, offset)
    topics: list[tuple[str, int]] = []
    while offset < len(packet):
        raw_topic, offset = _read_binary(packet, offset)
        if offset >= len(packet):
            raise ProtocolError("missing requested QoS in MQTT SUBSCRIBE")
        qos = packet[offset]
        offset += 1
        if qos > 2:
            raise ProtocolError("invalid requested QoS")
        try:
            topics.append((raw_topic.decode("utf-8"), qos))
        except UnicodeDecodeError as err:
            raise ProtocolError("MQTT topic is not UTF-8") from err
    if not topics:
        raise ProtocolError("empty MQTT SUBSCRIBE")
    return MqttSubscribe(packet_id, tuple(topics))


@dataclasses.dataclass(frozen=True)
class MqttPublish:
    topic: str
    payload: bytes
    qos: int
    packet_id: int | None


def parse_publish(packet: bytes) -> MqttPublish:
    if not packet or packet[0] >> 4 != 3:
        raise ProtocolError("packet is not MQTT PUBLISH")
    remaining, offset = decode_remaining_length(packet)
    if offset + remaining != len(packet):
        raise ProtocolError("MQTT PUBLISH length mismatch")
    qos = (packet[0] >> 1) & 0x03
    if qos == 3:
        raise ProtocolError("invalid MQTT PUBLISH QoS")
    raw_topic, offset = _read_binary(packet, offset)
    packet_id = None
    if qos:
        packet_id, offset = _read_u16(packet, offset)
    try:
        topic = raw_topic.decode("utf-8")
    except UnicodeDecodeError as err:
        raise ProtocolError("MQTT topic is not UTF-8") from err
    return MqttPublish(topic, packet[offset:], qos, packet_id)


def build_connack(return_code: int = 0) -> bytes:
    return bytes((0x20, 0x02, 0x00, return_code))


def build_suback(packet_id: int, granted_qos: Iterable[int]) -> bytes:
    payload = struct.pack("!H", packet_id) + bytes(granted_qos)
    return b"\x90" + encode_remaining_length(len(payload)) + payload


def build_puback(packet_id: int) -> bytes:
    return b"\x40\x02" + struct.pack("!H", packet_id)


def build_publish(topic: str, payload: bytes, packet_id: int = 1, qos: int = 1) -> bytes:
    if qos not in (0, 1):
        raise ValueError("bootstrap publisher supports QoS 0 or 1")
    variable = encode_utf8(topic)
    if qos:
        if not 1 <= packet_id <= 65535:
            raise ValueError("packet_id must be nonzero")
        variable += struct.pack("!H", packet_id)
    body = variable + payload
    return bytes((0x30 | (qos << 1),)) + encode_remaining_length(len(body)) + body


def build_ota_command(
    *, msg_id: str, artifact_url: str, artifact_md5: str, target_version: str
) -> bytes:
    if len(artifact_url.encode("ascii")) > 256:
        raise ValueError("OTA artifact URL exceeds firmware limit")
    if len(artifact_md5) != 32 or any(c not in "0123456789abcdef" for c in artifact_md5):
        raise ValueError("artifact_md5 must be 32 lowercase hexadecimal characters")
    if not target_version.startswith("3") or len(target_version) > 31:
        raise ValueError("target_version must be a 3.x firmware-family value")
    document = {
        "cmd": "OTA_UPGRADE",
        "msgId": msg_id,
        "ts": 0,
        "upgradeType": 2,
        "url": artifact_url,
        "targetSoftwareVersion": target_version,
        "md5": artifact_md5,
    }
    return json.dumps(document, separators=(",", ":")).encode("ascii")
