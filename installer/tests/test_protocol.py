from __future__ import annotations

import struct
import unittest

from installer.protocol import (
    ProtocolError,
    build_ota_command,
    build_set_mqtt_server_frame,
    encode_remaining_length,
    encode_utf8,
    parse_connect,
    parse_publish,
    parse_subscribe,
)


class ProvisioningProtocolTests(unittest.TestCase):
    def test_builds_normal_cmd02_frame(self) -> None:
        frame = build_set_mqtt_server_frame("192.0.2.10", 1883, "1", sequence=7)
        endpoint = b"192.0.2.10:1883"
        self.assertEqual(b"DL\x02\x00\x07", frame[:5])
        self.assertEqual(len(endpoint) + 3, frame[5])
        self.assertEqual(len(endpoint), frame[6])
        self.assertEqual(endpoint, frame[7 : 7 + len(endpoint)])
        self.assertEqual(b"\x01\x31\xff", frame[-3:])

    def test_rejects_colon_in_host(self) -> None:
        with self.assertRaisesRegex(ValueError, "without ':'"):
            build_set_mqtt_server_frame("host:bad", 1883)

    def test_rejects_ports_the_oem_parser_cannot_split(self) -> None:
        for port in (999, 10000, 65535):
            with self.subTest(port=port), self.assertRaisesRegex(ValueError, "four decimal"):
                build_set_mqtt_server_frame("192.0.2.10", port)


class MqttProtocolTests(unittest.TestCase):
    def test_parses_feeder_connect_credentials(self) -> None:
        flags = 0xC2
        variable = b"\x00\x04MQTT\x04" + bytes((flags,)) + b"\x00\x3c"
        payload = encode_utf8("SERIAL123") + encode_utf8("product-key") + encode_utf8(b"secret")
        packet = b"\x10" + encode_remaining_length(len(variable + payload)) + variable + payload
        result = parse_connect(packet)
        self.assertEqual("SERIAL123", result.client_id)
        self.assertEqual("product-key", result.username)
        self.assertEqual(b"secret", result.password)
        self.assertEqual(60, result.keep_alive)

    def test_rejects_mqtt_v5_connect(self) -> None:
        body = b"\x00\x04MQTT\x05\x02\x00\x3c\x00\x00\x00"
        with self.assertRaisesRegex(ProtocolError, "3.1.1"):
            parse_connect(b"\x10" + encode_remaining_length(len(body)) + body)

    def test_parses_subscribe_and_publish(self) -> None:
        topic = "dl/PLAF203/SERIAL/device/ota/sub"
        subscribe_body = struct.pack("!H", 9) + encode_utf8(topic) + b"\x01"
        subscription = parse_subscribe(
            b"\x82" + encode_remaining_length(len(subscribe_body)) + subscribe_body
        )
        self.assertEqual(9, subscription.packet_id)
        self.assertEqual(((topic, 1),), subscription.topics)

        publish_body = encode_utf8("post/topic") + struct.pack("!H", 12) + b'{"ok":true}'
        publication = parse_publish(
            b"\x32" + encode_remaining_length(len(publish_body)) + publish_body
        )
        self.assertEqual("post/topic", publication.topic)
        self.assertEqual(12, publication.packet_id)
        self.assertEqual(b'{"ok":true}', publication.payload)

    def test_ota_command_is_compact_and_exact(self) -> None:
        result = build_ota_command(
            msg_id="bootstrap-1",
            artifact_url="http://192.0.2.10:18080/token",
            artifact_md5="0" * 32,
            target_version="3.99.99",
        )
        self.assertIn(b'"cmd":"OTA_UPGRADE"', result)
        self.assertIn(b'"upgradeType":2', result)
        self.assertIn(b'"md5":"' + b"0" * 32 + b'"', result)

    def test_ota_command_rejects_wrong_firmware_family(self) -> None:
        with self.assertRaisesRegex(ValueError, "3.x"):
            build_ota_command(
                msg_id="bootstrap-1",
                artifact_url="http://192.0.2.10:18080/token",
                artifact_md5="0" * 32,
                target_version="4.0.0",
            )


if __name__ == "__main__":
    unittest.main()
