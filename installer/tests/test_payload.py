from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from installer.payload import build_payload, validate_public_key


VALID_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "bootstrap@example"
)


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

    def _dropbear_tree(self, root: Path) -> Path:
        dropbear = root / "dropbear-build"
        dropbear.mkdir()
        elf32_arm = bytearray(52)
        elf32_arm[:7] = b"\x7fELF\x01\x01\x01"
        elf32_arm[18:20] = (40).to_bytes(2, "little")
        elf32_arm[36:40] = (0x400).to_bytes(4, "little")
        (dropbear / "dropbearmulti").write_bytes(elf32_arm)
        (dropbear / "LICENSE.dropbear").write_text("Dropbear license\n")
        (dropbear / "BUILD_INFO.dropbear.json").write_text("{}\n")
        return dropbear

    def _run_installer(
        self, execution_slot: str
    ) -> tuple[Path, bytes, tempfile.TemporaryDirectory[str]]:
        raw = tempfile.TemporaryDirectory()
        temp = Path(raw.name)
        agent = self._state_agent_tree(temp)
        payload = temp / "payload"
        build_payload(
            output=payload,
            state_agent_dir=agent,
            home_assistant_ip="192.0.2.25",
            state_agent_token="a" * 64,
            ssh_public_key=VALID_PUBLIC_KEY,
            dropbear_dir=self._dropbear_tree(temp),
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

    def test_installer_always_enables_key_only_dropbear(self) -> None:
        feeder, _donor, temporary = self._run_installer("ota2")
        with temporary:
            data = feeder / "data"
            self.assertTrue((data / "enable_ssh").is_file())
            self.assertEqual(
                VALID_PUBLIC_KEY + "\n",
                (data / "dropbear" / "authorized_keys").read_text(),
            )
            app_start = (feeder / "app_start.sh").read_text()
            self.assertIn("-p 2222 -s", app_start)
            self.assertNotIn("pidof dropbear", app_start)
            self.assertIn("/proc/$dropbear_pid/cmdline", app_start)
            self.assertNotIn("telnet", app_start)
            self.assertTrue((data / "dropbear" / "dropbear").is_symlink())
            self.assertEqual(
                "dropbearmulti", os.readlink(data / "dropbear" / "dropbear")
            )
            self.assertEqual(
                "Dropbear license\n",
                (data / "dropbear" / "LICENSE.dropbear").read_text(),
            )
            self.assertEqual(
                "{}\n",
                (data / "dropbear" / "BUILD_INFO.dropbear.json").read_text(),
            )

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

    def test_state_agent_startup_matches_only_its_runsvdir_tree(self) -> None:
        snippet = (
            Path(__file__).resolve().parents[2]
            / "state-agent"
            / "app_start_snippet.sh"
        ).read_text()
        self.assertNotIn("pidof runsvdir", snippet)
        self.assertIn("runsvdir_for_tree_running", snippet)

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
                ssh_public_key=VALID_PUBLIC_KEY,
                dropbear_dir=self._dropbear_tree(temp),
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
                ssh_public_key=VALID_PUBLIC_KEY,
                dropbear_dir=self._dropbear_tree(temp),
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

    def test_ssh_key_is_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            with self.assertRaisesRegex(ValueError, "SSH public key is required"):
                build_payload(
                    output=temp / "payload",
                    state_agent_dir=self._state_agent_tree(temp),
                    home_assistant_ip="192.0.2.25",
                    state_agent_token="a" * 64,
                    ssh_public_key="",
                    dropbear_dir=self._dropbear_tree(temp),
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
                    state_agent_token="a" * 64,
                    ssh_public_key=VALID_PUBLIC_KEY,
                    dropbear_dir=self._dropbear_tree(temp),
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
                state_agent_token="a" * 64,
                ssh_public_key=VALID_PUBLIC_KEY,
                dropbear_dir=self._dropbear_tree(root),
            )

            self.assertTrue(output.is_file())

    def test_rejects_malformed_public_key_blob(self) -> None:
        with self.assertRaisesRegex(ValueError, "base64"):
            validate_public_key("ssh-ed25519 not-base64 user@example")

    def test_rejects_public_key_type_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_public_key(VALID_PUBLIC_KEY.replace("ssh-ed25519", "ssh-rsa", 1))

    def test_rejects_invalid_state_agent_token_shape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(ValueError, "64 lowercase hexadecimal"):
                build_payload(
                    output=root / "payload",
                    state_agent_dir=self._state_agent_tree(root),
                    home_assistant_ip="192.0.2.25",
                    state_agent_token="not-a-token",
                    ssh_public_key=VALID_PUBLIC_KEY,
                    dropbear_dir=self._dropbear_tree(root),
                )


if __name__ == "__main__":
    unittest.main()
