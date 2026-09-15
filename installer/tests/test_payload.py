from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from installer.payload import build_payload


class PayloadTests(unittest.TestCase):
    def _state_agent_tree(self, root: Path) -> Path:
        agent = root / "agent"
        (agent / "runit" / "plaf203-state-agent").mkdir(parents=True)
        (agent / "runit" / "plaf203-update-supervisor").mkdir(parents=True)
        (agent / "plaf203-state-agent").write_bytes(b"agent-binary")
        (agent / "plaf203-update-fs").write_bytes(b"helper-binary")
        (agent / "app_start_snippet.sh").write_text("#!/bin/sh\n", encoding="ascii")
        (agent / "runit" / "plaf203-state-agent" / "run").write_text(
            "#!/bin/sh\nexec agent --allow-ip 192.0.2.10\n", encoding="ascii"
        )
        (agent / "runit" / "plaf203-update-supervisor" / "run").write_text(
            "#!/bin/sh\nexit 0\n", encoding="ascii"
        )
        (agent / "runit" / "plaf203-update-supervisor" / "supervisor.sh").write_text(
            "#!/bin/sh\nexit 0\n", encoding="ascii"
        )
        return agent

    def _run_installer(
        self, execution_slot: str, *, enable_ssh: bool = False
    ) -> tuple[Path, bytes, tempfile.TemporaryDirectory[str]]:
        raw = tempfile.TemporaryDirectory()
        temp = Path(raw.name)
        agent = self._state_agent_tree(temp)
        payload = temp / "payload"
        dropbear = None
        key = ""
        if enable_ssh:
            dropbear = temp / "dropbear-build"
            dropbear.mkdir()
            elf32_arm = bytearray(52)
            elf32_arm[:7] = b"\x7fELF\x01\x01\x01"
            elf32_arm[18:20] = (40).to_bytes(2, "little")
            for name in ("dropbear", "dropbearkey"):
                (dropbear / name).write_bytes(elf32_arm)
            key = "ssh-ed25519 AAAATEST bootstrap@example"
        build_payload(
            output=payload,
            state_agent_dir=agent,
            home_assistant_ip="192.0.2.25",
            state_agent_token="a" * 64,
            ssh_public_key=key,
            dropbear_dir=dropbear,
        )
        feeder = temp / "feeder"
        (feeder / "ota1").mkdir(parents=True)
        (feeder / "ota2").mkdir()
        (feeder / "data" / "ota").mkdir(parents=True)
        donor = b"\x7fELF" + b"stock-oem" * 100
        donor_slot = "ota1" if execution_slot == "ota2" else "ota2"
        (feeder / donor_slot / "AF203_FW").write_bytes(donor)
        (feeder / donor_slot / "AF203_FW").chmod(0o755)
        (feeder / "app_start.sh").write_text(
            f"/user/{execution_slot}/AF203_FW &\n", encoding="ascii"
        )
        env = os.environ | {
            "PLAF203_BOOTSTRAP_TEST_ROOT": str(feeder),
            "PLAF203_BOOTSTRAP_TEST_SLOT": execution_slot,
        }
        subprocess.run(["/bin/sh", str(payload)], env=env, check=True, timeout=15)
        return feeder, donor, raw

    def test_installer_restores_ota2_and_selects_ota1(self) -> None:
        feeder, donor, temporary = self._run_installer("ota2")
        with temporary:
            self._assert_successful_install(feeder, donor)

    def test_installer_restores_ota1_and_selects_ota1(self) -> None:
        feeder, donor, temporary = self._run_installer("ota1")
        with temporary:
            self._assert_successful_install(feeder, donor)

    def test_installer_enables_key_only_dropbear_when_requested(self) -> None:
        feeder, _donor, temporary = self._run_installer("ota2", enable_ssh=True)
        with temporary:
            data = feeder / "data"
            self.assertTrue((data / "enable_ssh").is_file())
            self.assertEqual(
                "ssh-ed25519 AAAATEST bootstrap@example\n",
                (data / "dropbear" / "authorized_keys").read_text(),
            )
            app_start = (feeder / "app_start.sh").read_text()
            self.assertIn("-p 2222 -s", app_start)
            self.assertNotIn("telnet", app_start)

    def _assert_successful_install(self, feeder: Path, donor: bytes) -> None:
        self.assertEqual(donor, (feeder / "ota1" / "AF203_FW").read_bytes())
        self.assertEqual(donor, (feeder / "ota2" / "AF203_FW").read_bytes())
        self.assertEqual(
            b"\x00\x00\x00\x00", (feeder / "data" / "ota" / "index.bin").read_bytes()
        )
        self.assertEqual(
            "a" * 64,
            (feeder / "data" / "local-state-agent" / "token").read_text().strip(),
        )
        run = (
            feeder
            / "data"
            / "local-state-agent"
            / "runit"
            / "plaf203-state-agent"
            / "run"
        ).read_text()
        self.assertIn("--allow-ip 192.0.2.25", run)
        self.assertNotIn("192.0.2.10", run)
        app_start = (feeder / "app_start.sh").read_text()
        self.assertIn("/user/ota1/AF203_FW &", app_start)
        self.assertNotIn("telnet", app_start)
        self.assertNotIn("bootclock", app_start)
        self.assertNotIn("af203-live-log", app_start)
        self.assertEqual(
            "installed_rebooting_to_ota1\n",
            (feeder / "data" / "plaf203-bootstrap" / "status").read_text(),
        )

    def test_installer_rejects_unknown_test_slot_without_touching_startup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            agent = self._state_agent_tree(temp)
            payload = temp / "payload"
            build_payload(
                output=payload,
                state_agent_dir=agent,
                home_assistant_ip="192.0.2.25",
                state_agent_token="a" * 64,
            )
            feeder = temp / "feeder"
            (feeder / "ota1").mkdir(parents=True)
            (feeder / "ota2").mkdir()
            (feeder / "data" / "ota").mkdir(parents=True)
            donor = b"\x7fELF" + b"stock-oem" * 100
            (feeder / "ota1" / "AF203_FW").write_bytes(donor)
            (feeder / "ota1" / "AF203_FW").chmod(0o755)
            (feeder / "app_start.sh").write_text("/user/ota1/AF203_FW &\n", encoding="ascii")
            env = os.environ | {
                "PLAF203_BOOTSTRAP_TEST_ROOT": str(feeder),
                "PLAF203_BOOTSTRAP_TEST_SLOT": "invalid",
            }
            result = subprocess.run(
                ["/bin/sh", str(payload)],
                env=env,
                check=False,
                timeout=15,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(70, result.returncode)
            self.assertEqual(
                "/user/ota1/AF203_FW &\n", (feeder / "app_start.sh").read_text()
            )

    def test_failed_install_selects_the_untouched_donor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            payload = temp / "payload"
            build_payload(
                output=payload,
                state_agent_dir=self._state_agent_tree(temp),
                home_assistant_ip="192.0.2.25",
                state_agent_token="a" * 64,
            )
            feeder = temp / "feeder"
            (feeder / "ota1").mkdir(parents=True)
            (feeder / "ota2").mkdir()
            (feeder / "data" / "ota").mkdir(parents=True)
            donor = b"\x7fELF" + b"stock-oem" * 100
            (feeder / "ota2" / "AF203_FW").write_bytes(donor)
            (feeder / "ota2" / "AF203_FW").chmod(0o755)
            (feeder / "app_start.sh").write_text(
                "/user/ota1/AF203_FW &\n", encoding="ascii"
            )
            # A non-directory at the service destination forces a failure after
            # the transaction has committed its donor fallback.
            (feeder / "data" / "local-state-agent").write_text("conflict")
            env = os.environ | {
                "PLAF203_BOOTSTRAP_TEST_ROOT": str(feeder),
                "PLAF203_BOOTSTRAP_TEST_SLOT": "ota1",
            }
            result = subprocess.run(
                ["/bin/sh", str(payload)],
                env=env,
                check=False,
                timeout=15,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(
                b"\x01\x00\x00\x00", (feeder / "data" / "ota" / "index.bin").read_bytes()
            )
            self.assertIn(
                str(feeder / "ota2" / "AF203_FW"),
                (feeder / "app_start.sh").read_text(),
            )
            self.assertIn(
                "install_failed_installing_services",
                (feeder / "data" / "plaf203-bootstrap" / "status").read_text(),
            )

    def test_ssh_requires_dropbear_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            with self.assertRaisesRegex(ValueError, "dropbear_dir"):
                build_payload(
                    output=temp / "payload",
                    state_agent_dir=self._state_agent_tree(temp),
                    home_assistant_ip="192.0.2.25",
                    state_agent_token="token",
                    ssh_public_key="ssh-ed25519 AAAATEST user@example",
                )

    def test_state_agent_template_must_contain_expected_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            agent = self._state_agent_tree(temp)
            run = agent / "runit" / "plaf203-state-agent" / "run"
            run.write_text("#!/bin/sh\nexec agent --allow-ip 198.51.100.10\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "expected allowlist"):
                build_payload(
                    output=temp / "payload",
                    state_agent_dir=agent,
                    home_assistant_ip="192.0.2.25",
                    state_agent_token="token",
                )

    def test_template_address_is_itself_a_valid_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_agent = self._state_agent_tree(root)

            output = root / "AF203_FW.bootstrap"
            build_payload(
                output=output,
                state_agent_dir=state_agent,
                home_assistant_ip="192.0.2.10",
                state_agent_token="token",
            )

            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
