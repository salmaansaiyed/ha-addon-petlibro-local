from __future__ import annotations

import json
import unittest

from installer.mqtt_server import BootstrapMqttServer
from installer.protocol import MqttConnect, build_ota_command


class FakeSession:
    def __init__(self, subscriptions: set[str], *, generation: int = 1) -> None:
        self.subscriptions = subscriptions
        self.connect = MqttConnect("SERIAL", "user", b"password", 90, 4)
        self.connection_generation = generation
        self.published: list[tuple[str, bytes, int, int]] = []

    def publish(self, topic: str, payload: bytes, packet_id: int = 1, qos: int = 1) -> None:
        self.published.append((topic, payload, packet_id, qos))


class BootstrapMqttServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = BootstrapMqttServer(("127.0.0.1", 0), expected_feeder_ip=None)

    def tearDown(self) -> None:
        self.server.server_close()

    def test_start_event_records_version_and_gets_correlated_qos0_response(self) -> None:
        post = "dl/PLAF203/SERIAL/device/event/post"
        sub = "dl/PLAF203/SERIAL/device/event/sub"
        session = FakeSession({sub})
        payload = json.dumps(
            {
                "cmd": "DEVICE_START_EVENT",
                "msgId": "boot-1",
                "softwareVersion": "3.1.48",
            }
        ).encode()

        self.server._record_publication(session, post, payload)  # type: ignore[arg-type]

        self.assertTrue(self.server.device_start_ready.is_set())
        self.assertEqual("3.1.48", self.server.software_version)
        self.assertEqual(1, len(session.published))
        topic, response, _packet_id, qos = session.published[0]
        self.assertEqual(sub, topic)
        self.assertEqual(0, qos)
        self.assertEqual("boot-1", json.loads(response)["msgId"])

    def test_start_response_waits_for_the_matching_subscription(self) -> None:
        post = "dl/PLAF203/SERIAL/device/event/post"
        sub = "dl/PLAF203/SERIAL/device/event/sub"
        session = FakeSession(set())
        self.server._record_publication(  # type: ignore[arg-type]
            session,
            post,
            b'{"cmd":"DEVICE_START_EVENT","msgId":"boot-1","softwareVersion":"3.1.48"}',
        )
        self.assertEqual([], session.published)
        session.subscriptions.add(sub)
        self.server._subscriptions_changed(session)  # type: ignore[arg-type]
        self.assertEqual(sub, session.published[0][0])

    def test_ntp_request_gets_noncalibrating_stock_shaped_response(self) -> None:
        post = "dl/PLAF203/SERIAL/device/ntp/post"
        sub = "dl/PLAF203/SERIAL/device/ntp/sub"
        session = FakeSession({sub})

        self.server._record_publication(  # type: ignore[arg-type]
            session, post, b'{"cmd":"NTP","ts":123}'
        )

        self.assertEqual(1, len(session.published))
        topic, response, _packet_id, qos = session.published[0]
        self.assertEqual(sub, topic)
        self.assertEqual(0, qos)
        value = json.loads(response)
        self.assertEqual("NTP", value["cmd"])
        self.assertEqual(0, value["code"])
        self.assertIs(False, value["calibrationTag"])
        self.assertEqual(0, value["timezoneOffsetSeconds"])
        self.assertEqual(0, value["nextDSTTransitionTs"])
        self.assertNotIn("msgId", value)

    def test_ntp_response_waits_for_matching_subscription(self) -> None:
        post = "dl/PLAF203/SERIAL/device/ntp/post"
        sub = "dl/PLAF203/SERIAL/device/ntp/sub"
        session = FakeSession(set())

        self.server._record_publication(  # type: ignore[arg-type]
            session, post, b'{"cmd":"NTP","ts":123}'
        )
        self.assertEqual([], session.published)

        session.subscriptions.add(sub)
        self.server._subscriptions_changed(session)  # type: ignore[arg-type]
        self.assertEqual(sub, session.published[0][0])

    def test_ntp_sync_cannot_be_mistaken_for_initial_ntp_request(self) -> None:
        post = "dl/PLAF203/SERIAL/device/ntp/post"
        sub = "dl/PLAF203/SERIAL/device/ntp/sub"
        session = FakeSession({sub})

        self.server._record_publication(  # type: ignore[arg-type]
            session, post, b'{"cmd":"NTP_SYNC","msgId":"sync-1","code":0}'
        )

        self.assertEqual([], session.published)

    def test_start_event_on_wrong_topic_cannot_prove_oem_startup(self) -> None:
        session = FakeSession(set())

        self.server._record_publication(  # type: ignore[arg-type]
            session,
            "dl/PLAF203/SERIAL/device/ota/post",
            b'{"cmd":"DEVICE_START_EVENT","softwareVersion":"3.1.48"}',
        )

        self.assertFalse(self.server.device_start_ready.is_set())

    def test_ota_terminal_result_is_correlated_by_message_id(self) -> None:
        ota_sub = "dl/PLAF203/SERIAL/device/ota/sub"
        ota_post = "dl/PLAF203/SERIAL/device/ota/post"
        session = FakeSession({ota_sub})
        self.server.session = session  # type: ignore[assignment]
        command = build_ota_command(
            msg_id="ota-current",
            artifact_url="http://192.0.2.10:18080/payload",
            artifact_md5="0" * 32,
            target_version="3.1.48",
        )
        self.assertEqual(ota_sub, self.server.publish_ota(command))

        self.server._record_publication(  # type: ignore[arg-type]
            session, ota_post, b'{"cmd":"OTA_INFORM","msgId":"ota-old","state":1}'
        )
        self.assertFalse(self.server.ota_result.is_set())

        self.server._record_publication(  # type: ignore[arg-type]
            session,
            ota_post,
            b'{"cmd":"OTA_INFORM","msgId":"ota-current","state":2,"errorMsg":"bad md5"}',
        )
        self.assertTrue(self.server.ota_result.is_set())
        self.assertEqual("bad md5", self.server.ota_error)

    def test_post_ota_start_requires_a_new_connection_generation(self) -> None:
        ota_sub = "dl/PLAF203/SERIAL/device/ota/sub"
        event_post = "dl/PLAF203/SERIAL/device/event/post"
        command = build_ota_command(
            msg_id="ota-current",
            artifact_url="http://192.0.2.10:18080/payload",
            artifact_md5="0" * 32,
            target_version="3.1.48",
        )
        original = FakeSession({ota_sub}, generation=4)
        self.server.session = original  # type: ignore[assignment]
        self.server.publish_ota(command)

        startup = b'{"cmd":"DEVICE_START_EVENT","msgId":"boot","softwareVersion":"3.1.48"}'
        self.server._record_publication(original, event_post, startup)  # type: ignore[arg-type]
        self.assertFalse(self.server.post_ota_start_ready.is_set())

        restored = FakeSession(set(), generation=5)
        self.server._record_publication(restored, event_post, startup)  # type: ignore[arg-type]
        self.assertTrue(self.server.post_ota_start_ready.is_set())

    def test_ota_puback_requires_the_sending_connection_generation(self) -> None:
        ota_sub = "dl/PLAF203/SERIAL/device/ota/sub"
        command = build_ota_command(
            msg_id="ota-current",
            artifact_url="http://192.0.2.10:18080/payload",
            artifact_md5="0" * 32,
            target_version="3.1.48",
        )
        sending = FakeSession({ota_sub}, generation=4)
        self.server.session = sending  # type: ignore[assignment]
        self.server.publish_ota(command)

        self.server._record_puback(FakeSession(set(), generation=3), 1)  # type: ignore[arg-type]
        self.assertFalse(self.server.ota_publish_acked.is_set())
        self.server._record_puback(sending, 1)  # type: ignore[arg-type]
        self.assertTrue(self.server.ota_publish_acked.is_set())


if __name__ == "__main__":
    unittest.main()
