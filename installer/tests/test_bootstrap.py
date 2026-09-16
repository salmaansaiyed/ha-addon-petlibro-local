from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from installer.bootstrap import (
    DEFAULT_STATE_AGENT_MANIFEST_URL,
    OEM_PROVISIONING_MEMBER_ID,
    BootstrapConfig,
    BootstrapPaused,
    _acquire_feeder_identity,
    _load_or_create_state_agent_token,
    _read_recovery_key,
    _repository_path,
    _restore_final_broker_and_verify_heartbeat,
    _send_provisioning,
    _select_target_version,
    _validate_manifest_url,
    _wait_for_final_broker,
    _write_private_text,
)


VALID_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "test@example"
)


def config(**overrides) -> BootstrapConfig:
    values = {
        "feeder_ip": "192.0.2.20",
        "bootstrap_host_ip": "192.0.2.10",
        "home_assistant_ip": "192.0.2.10",
        "feeder_mqtt_host": "192.0.2.10",
        "backend_mqtt_username": "backend",
        "backend_mqtt_password": "secret",
        "ssh_public_key": VALID_PUBLIC_KEY,
    }
    values.update(overrides)
    return BootstrapConfig(**values)


class BootstrapConfigTests(unittest.TestCase):
    def test_validates_production_defaults(self) -> None:
        config().validate()

    def test_private_output_is_created_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "protected.json"
            _write_private_text(output, '{"secret":"value"}\n')
            self.assertEqual(0o600, output.stat().st_mode & 0o777)
            self.assertEqual('{"secret":"value"}\n', output.read_text(encoding="utf-8"))

    def test_final_broker_must_use_firmware_mqtt_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be 1883"):
            config(feeder_mqtt_port=1884).validate()

    def test_bootstrap_broker_must_use_firmware_mqtt_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be 1883"):
            config(bootstrap_mqtt_port=1884).validate()

    def test_recovery_ssh_key_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "SSH recovery access"):
            config(ssh_public_key="").validate()

    def test_default_release_manifest_is_https(self) -> None:
        self.assertEqual(
            DEFAULT_STATE_AGENT_MANIFEST_URL,
            _validate_manifest_url(DEFAULT_STATE_AGENT_MANIFEST_URL),
        )

    def test_load_ignores_legacy_mqtt_and_manifest_fields(self) -> None:
        document = asdict(config())
        document["backend_mqtt_host"] = "core-mosquitto"
        document["feeder_mqtt_member_id"] = "190489799"
        document["state_agent_manifest_url"] = "https://legacy.invalid/latest.json"
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "bootstrap.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            loaded = BootstrapConfig.load(path)

        self.assertEqual("192.0.2.10", loaded.feeder_mqtt_host)
        self.assertFalse(hasattr(loaded, "backend_mqtt_host"))
        self.assertFalse(hasattr(loaded, "feeder_mqtt_member_id"))
        self.assertFalse(hasattr(loaded, "state_agent_manifest_url"))

    def test_manifest_launch_option_must_use_https(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be HTTPS"):
            _validate_manifest_url("http://updates.invalid/latest.json")

    def test_manifest_launch_option_rejects_credentials_query_and_fragment(self) -> None:
        for value in (
            "https://user:password@updates.invalid/latest.json",
            "https://updates.invalid/latest.json?channel=stable",
            "https://updates.invalid/latest.json#latest",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "without"):
                _validate_manifest_url(value)

    def test_recovery_key_accepts_raw_key_or_file(self) -> None:
        self.assertEqual(VALID_PUBLIC_KEY, _read_recovery_key(VALID_PUBLIC_KEY))
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "recovery.pub"
            path.write_text(VALID_PUBLIC_KEY + "\n", encoding="ascii")
            self.assertEqual(VALID_PUBLIC_KEY, _read_recovery_key(str(path)))

    def test_state_agent_token_is_bound_to_feeder_and_home_assistant(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            first = _load_or_create_state_agent_token(config(), output)
            self.assertRegex(first, r"^[0-9a-f]{64}$")
            self.assertEqual(first, _load_or_create_state_agent_token(config(), output))
            with self.assertRaisesRegex(ValueError, "different feeder"):
                _load_or_create_state_agent_token(
                    config(feeder_ip="192.0.2.21"), output
                )
            rotated = _load_or_create_state_agent_token(
                config(feeder_ip="192.0.2.21"), output, rotate=True
            )
            self.assertNotEqual(first, rotated)

    def test_invalid_existing_state_agent_token_requires_explicit_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            (output / "state-agent-token.txt").write_text("invalid\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "--rotate-state-agent-token"):
                _load_or_create_state_agent_token(config(), output)

    def test_existing_state_agent_token_rejects_extra_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            (output / "state-agent-token.txt").write_text(" " + "a" * 64 + "\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "--rotate-state-agent-token"):
                _load_or_create_state_agent_token(config(), output)

    def test_uses_observed_firmware_version_by_default(self) -> None:
        self.assertEqual("3.1.48", _select_target_version(config(), "3.1.48"))

    def test_explicit_target_version_is_an_override(self) -> None:
        value = config(target_software_version="3.1.49")
        value.validate()
        self.assertEqual("3.1.49", _select_target_version(value, "3.1.48"))

    def test_requires_a_valid_startup_version_even_with_override(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "DEVICE_START_EVENT"):
            _select_target_version(config(target_software_version="3.1.49"), None)

    def test_generated_paths_must_remain_in_repository(self) -> None:
        with self.assertRaisesRegex(ValueError, "inside the repository"):
            _repository_path(Path("/tmp/plaf203-output"), "test output")

    @patch("installer.bootstrap.build_set_mqtt_server_frame", return_value=b"frame")
    @patch("installer.bootstrap.socket.socket")
    def test_provisioning_command_is_sent_once(self, socket_factory, build_frame) -> None:
        udp = socket_factory.return_value.__enter__.return_value

        _send_provisioning(config(), "192.0.2.10", 1883)

        build_frame.assert_called_once_with("192.0.2.10", 1883, OEM_PROVISIONING_MEMBER_ID)
        udp.sendto.assert_called_once()
        self.assertEqual(("192.0.2.20", 17484), udp.sendto.call_args.args[1])

    @patch("installer.bootstrap._wait_for_feeder_identity")
    @patch("installer.bootstrap._send_provisioning")
    def test_already_redirected_feeder_skips_provisioning(self, send, wait_for_identity) -> None:
        mqtt = SimpleNamespace(identity_ready=SimpleNamespace(wait=lambda _timeout: True))

        _acquire_feeder_identity(mqtt, config(), reconnect_grace=0)

        send.assert_not_called()
        wait_for_identity.assert_not_called()

    @patch("installer.bootstrap._wait_for_feeder_identity")
    @patch("installer.bootstrap._send_provisioning")
    def test_stock_feeder_is_provisioned_after_reconnect_grace(
        self, send, wait_for_identity
    ) -> None:
        mqtt = SimpleNamespace(identity_ready=SimpleNamespace(wait=lambda _timeout: False))
        value = config()

        _acquire_feeder_identity(mqtt, value, reconnect_grace=0)

        send.assert_called_once_with(value, value.bootstrap_host_ip, value.bootstrap_mqtt_port)
        wait_for_identity.assert_called_once_with(mqtt, value)

    @patch("installer.bootstrap.select", return_value=0)
    @patch(
        "installer.bootstrap.probe_mqtt_credentials",
        side_effect=[ConnectionRefusedError("not configured"), None],
    )
    def test_final_broker_setup_retries_in_same_session(self, probe, menu) -> None:
        identity = SimpleNamespace(
            client_id="client", username="user", password=b"secret"
        )
        _wait_for_final_broker(config(), identity, Path("credentials.json"))
        self.assertEqual(2, probe.call_count)
        menu.assert_called_once()

    @patch("installer.bootstrap.select", return_value=1)
    @patch(
        "installer.bootstrap.probe_mqtt_credentials",
        side_effect=ConnectionRefusedError("not configured"),
    )
    def test_final_broker_setup_can_pause_before_ota(self, _probe, _menu) -> None:
        identity = SimpleNamespace(
            client_id="client", username="user", password=b"secret"
        )
        with self.assertRaises(BootstrapPaused):
            _wait_for_final_broker(config(), identity, Path("credentials.json"))

    @patch("installer.bootstrap._send_provisioning")
    @patch("installer.bootstrap.wait_for_mqtt_heartbeat")
    def test_final_broker_heartbeat_is_verified_after_subscription(
        self, wait_heartbeat, send
    ) -> None:
        wait_heartbeat.side_effect = lambda **kwargs: kwargs["on_subscribed"]()
        value = config()

        verified = _restore_final_broker_and_verify_heartbeat(
            value, SimpleNamespace(client_id="AF0301"), timeout=1
        )

        self.assertTrue(verified)
        send.assert_called_once_with(value, value.feeder_mqtt_host, value.feeder_mqtt_port)
        self.assertEqual(value.backend_mqtt_username, wait_heartbeat.call_args.kwargs["username"])

    @patch("installer.bootstrap._send_provisioning")
    @patch(
        "installer.bootstrap.wait_for_mqtt_heartbeat",
        side_effect=ConnectionRefusedError("broker unavailable"),
    )
    def test_final_broker_verification_failure_still_restores_endpoint(
        self, _wait_heartbeat, send
    ) -> None:
        value = config()

        verified = _restore_final_broker_and_verify_heartbeat(
            value, SimpleNamespace(client_id="AF0301"), timeout=1
        )

        self.assertFalse(verified)
        send.assert_called_once_with(value, value.feeder_mqtt_host, value.feeder_mqtt_port)

    @patch("installer.bootstrap._send_provisioning")
    @patch("installer.bootstrap.wait_for_mqtt_heartbeat")
    def test_heartbeat_timeout_does_not_repeat_endpoint_command(
        self, wait_heartbeat, send
    ) -> None:
        def timeout_after_redirect(**kwargs):
            kwargs["on_subscribed"]()
            raise TimeoutError("no heartbeat")

        wait_heartbeat.side_effect = timeout_after_redirect
        value = config()

        self.assertFalse(
            _restore_final_broker_and_verify_heartbeat(
                value, SimpleNamespace(client_id="AF0301"), timeout=1
            )
        )
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
