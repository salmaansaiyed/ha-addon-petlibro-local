from __future__ import annotations

import unittest

from installer.bootstrap import BootstrapConfig, _select_target_version


def config(**overrides) -> BootstrapConfig:
    values = {
        "feeder_ip": "192.0.2.20",
        "bootstrap_host_ip": "192.0.2.10",
        "home_assistant_ip": "192.0.2.10",
        "feeder_mqtt_host": "192.0.2.10",
        "backend_mqtt_username": "backend",
        "backend_mqtt_password": "secret",
    }
    values.update(overrides)
    return BootstrapConfig(**values)


class BootstrapConfigTests(unittest.TestCase):
    def test_validates_production_defaults(self) -> None:
        config().validate()

    def test_feeder_facing_ports_must_have_four_digits(self) -> None:
        for field, value in (("feeder_mqtt_port", 10000), ("bootstrap_mqtt_port", 999)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "four digits"):
                config(**{field: value}).validate()

    def test_uses_observed_firmware_version_by_default(self) -> None:
        self.assertEqual("3.1.48", _select_target_version(config(), "3.1.48"))

    def test_explicit_target_version_is_an_override(self) -> None:
        value = config(target_software_version="3.1.49")
        value.validate()
        self.assertEqual("3.1.49", _select_target_version(value, "3.1.48"))

    def test_requires_a_valid_startup_version_even_with_override(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "DEVICE_START_EVENT"):
            _select_target_version(config(target_software_version="3.1.49"), None)


if __name__ == "__main__":
    unittest.main()
