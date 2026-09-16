from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from installer.dropbear import (
    BUILD_SCHEMA,
    DROPBEAR_COMMIT,
    DROPBEAR_REPOSITORY,
    DROPBEAR_TAG,
    ZIG_VERSION,
    _validate_bundle,
    default_cache_dir,
)


class DropbearBuildTests(unittest.TestCase):
    def test_default_cache_stays_in_repository_build_tree(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        self.assertTrue(default_cache_dir().is_relative_to(repository / "build"))

    def test_pins_full_upstream_commit(self) -> None:
        self.assertRegex(DROPBEAR_COMMIT, r"^[0-9a-f]{40}$")

    def test_bundle_requires_arm_hard_float_binary_and_license(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            binary = bytearray(52)
            binary[:5] = b"\x7fELF\x01"
            binary[18:20] = (40).to_bytes(2, "little")
            (bundle / "dropbearmulti").write_bytes(binary)
            (bundle / "LICENSE.dropbear").write_text("license\n")
            manifest = {
                "schema": BUILD_SCHEMA,
                "repository": DROPBEAR_REPOSITORY,
                "tag": DROPBEAR_TAG,
                "commit": DROPBEAR_COMMIT,
                "zig_version": ZIG_VERSION,
                "target": "arm-linux-musleabihf",
                "cpu": "cortex_a7",
                "programs": ["dropbear", "dropbearkey"],
                "password_auth": False,
                "dropbearmulti_sha256": hashlib.sha256(binary).hexdigest(),
            }
            (bundle / "BUILD_INFO.dropbear.json").write_text(json.dumps(manifest))
            self.assertFalse(_validate_bundle(bundle))
            binary[36:40] = (0x400).to_bytes(4, "little")
            (bundle / "dropbearmulti").write_bytes(binary)
            manifest["dropbearmulti_sha256"] = hashlib.sha256(binary).hexdigest()
            (bundle / "BUILD_INFO.dropbear.json").write_text(json.dumps(manifest))
            self.assertTrue(_validate_bundle(bundle))

            binary[-1] = 1
            (bundle / "dropbearmulti").write_bytes(binary)
            self.assertFalse(_validate_bundle(bundle))


if __name__ == "__main__":
    unittest.main()
