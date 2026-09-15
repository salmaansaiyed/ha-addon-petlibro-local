#!/usr/bin/env python3
"""Guided no-UART bootstrap for an already claimed stock PLAF203 feeder."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import http.server
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

try:
    from .mqtt_server import BootstrapMqttServer, probe_mqtt_credentials, wait_for
    from .payload import build_payload
    from .protocol import build_ota_command, build_set_mqtt_server_frame
except ImportError:  # direct execution from installer
    from mqtt_server import BootstrapMqttServer, probe_mqtt_credentials, wait_for
    from payload import build_payload
    from protocol import build_ota_command, build_set_mqtt_server_frame


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_AGENT_DIR = REPO_ROOT / "state-agent"
DIRECT_URL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class BootstrapConfig:
    feeder_ip: str
    bootstrap_host_ip: str
    home_assistant_ip: str
    feeder_mqtt_host: str
    feeder_mqtt_port: int = 1883
    feeder_mqtt_member_id: str = "1"
    backend_mqtt_host: str = "core-mosquitto"
    backend_mqtt_port: int = 1883
    backend_mqtt_username: str = ""
    backend_mqtt_password: str = ""
    bootstrap_mqtt_port: int = 1884
    bootstrap_http_port: int = 18080
    state_agent_manifest_url: str = ""
    state_agent_dir: str = str(DEFAULT_STATE_AGENT_DIR)
    dropbear_dir: str = ""
    ssh_public_key: str = ""
    target_software_version: str = ""
    output_dir: str = "./plaf203-bootstrap-output"

    @classmethod
    def load(cls, path: Path) -> "BootstrapConfig":
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("bootstrap config must be a JSON object")
        allowed = {field.name for field in fields(cls)}
        unknown = set(document) - allowed
        if unknown:
            raise ValueError(f"unknown bootstrap config fields: {', '.join(sorted(unknown))}")
        config = cls(**document)
        config.validate()
        return config

    def validate(self) -> None:
        for name in ("feeder_ip", "bootstrap_host_ip", "home_assistant_ip"):
            value = ipaddress.ip_address(getattr(self, name))
            if value.version != 4 or value.is_unspecified or value.is_multicast:
                raise ValueError(f"{name} must be a usable IPv4 address")
        # The OEM parser splits on ':' and its downloader supports direct IPv4.
        broker = ipaddress.ip_address(self.feeder_mqtt_host)
        if broker.version != 4:
            raise ValueError("feeder_mqtt_host must be an IPv4 address")
        for name in (
            "feeder_mqtt_port",
            "bootstrap_mqtt_port",
        ):
            if not 1000 <= int(getattr(self, name)) <= 9999:
                raise ValueError(
                    f"{name} must contain exactly four digits due to the OEM parser"
                )
        for name in ("backend_mqtt_port", "bootstrap_http_port"):
            if not 1 <= int(getattr(self, name)) <= 65535:
                raise ValueError(f"{name} must be between 1 and 65535")
        if not self.feeder_mqtt_member_id.isdecimal():
            raise ValueError("feeder_mqtt_member_id must contain decimal digits")
        if not self.backend_mqtt_username or not self.backend_mqtt_password:
            raise ValueError("backend MQTT username and password are required")
        if self.target_software_version and not re.fullmatch(
            r"3\.[0-9]+\.[0-9]+", self.target_software_version
        ):
            raise ValueError("target_software_version must be an optional 3.x.y value")
        if self.state_agent_manifest_url and not self.state_agent_manifest_url.startswith("https://"):
            raise ValueError("state_agent_manifest_url must be HTTPS")


def _prompt(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    reader = getpass.getpass if secret else input
    value = reader(f"{label}{suffix}: ").strip()
    return value or default


def configure(output: Path) -> None:
    print("PLAF203 no-UART bootstrap configuration")
    print("All devices must be on the same trusted LAN. Secrets are written mode 0600.\n")
    ssh_key_path = _prompt("SSH public-key file (optional)")
    ssh_key = Path(ssh_key_path).expanduser().read_text(encoding="ascii").strip() if ssh_key_path else ""
    dropbear_dir = _prompt("Directory containing static ARM dropbear/dropbearkey", "") if ssh_key else ""
    config = BootstrapConfig(
        feeder_ip=_prompt("Feeder IPv4 address"),
        bootstrap_host_ip=_prompt("This setup computer's LAN IPv4 address"),
        home_assistant_ip=_prompt("Home Assistant IPv4 address"),
        feeder_mqtt_host=_prompt("Final MQTT broker LAN IPv4 address"),
        feeder_mqtt_port=int(_prompt("Final feeder MQTT port", "1883")),
        backend_mqtt_host=_prompt("Add-on MQTT host", "core-mosquitto"),
        backend_mqtt_port=int(_prompt("Add-on MQTT port", "1883")),
        backend_mqtt_username=_prompt("Add-on MQTT username"),
        backend_mqtt_password=_prompt("Add-on MQTT password", secret=True),
        bootstrap_mqtt_port=int(_prompt("Temporary bootstrap MQTT port", "1884")),
        bootstrap_http_port=int(_prompt("Temporary bootstrap HTTP port", "18080")),
        state_agent_manifest_url=_prompt("Signed State Agent latest.json HTTPS URL (optional)"),
        dropbear_dir=dropbear_dir,
        ssh_public_key=ssh_key,
        feeder_mqtt_member_id=_prompt(
            "Numeric OEM member ID (1 is sufficient for local-only operation)", "1"
        ),
        output_dir=str(output.parent / "plaf203-bootstrap-output"),
    )
    config.validate()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
    output.chmod(0o600)
    print(f"Wrote protected configuration to {output}")


def _ensure_arm_elf(path: Path) -> None:
    if not path.is_file() or path.read_bytes()[:4] != b"\x7fELF":
        raise ValueError(f"missing ARM ELF artifact: {path}")
    data = path.read_bytes()[:20]
    if len(data) < 20 or data[4] != 1 or int.from_bytes(data[18:20], "little") != 40:
        raise ValueError(f"artifact is not ELF32 ARM: {path}")


def prepare(config: BootstrapConfig) -> tuple[Path, str, Path]:
    output_dir = Path(config.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    state_agent_dir = Path(config.state_agent_dir).expanduser().resolve()
    for name in ("plaf203-state-agent", "plaf203-update-fs"):
        _ensure_arm_elf(state_agent_dir / name)
    token_path = output_dir / "state-agent-token.txt"
    if token_path.exists():
        token = token_path.read_text(encoding="ascii").strip()
    else:
        token = secrets.token_hex(32)
        token_path.write_text(token + "\n", encoding="ascii")
        token_path.chmod(0o600)
    payload = output_dir / "AF203_FW.bootstrap"
    build_payload(
        output=payload,
        state_agent_dir=state_agent_dir,
        home_assistant_ip=config.home_assistant_ip,
        state_agent_token=token,
        ssh_public_key=config.ssh_public_key,
        dropbear_dir=Path(config.dropbear_dir).expanduser().resolve() if config.dropbear_dir else None,
    )
    options_patch = {
        "mqtt_host": config.backend_mqtt_host,
        "mqtt_port": config.backend_mqtt_port,
        "mqtt_username": config.backend_mqtt_username,
        "mqtt_password": config.backend_mqtt_password,
        "persist_feeder_mqtt": True,
        "feeder_mqtt_host": config.feeder_mqtt_host,
        "feeder_mqtt_port": config.feeder_mqtt_port,
        "petlibro_state_agent_url": "http://{ip}:8765",
        "petlibro_state_agent_token": token,
        "state_agent_updates": {
            "enabled": bool(config.state_agent_manifest_url),
            "manifest_url": config.state_agent_manifest_url,
            "check_on_connect": True,
            "check_interval_hours": 24,
        },
    }
    patch_path = output_dir / "addon-options.patch.json"
    patch_path.write_text(json.dumps(options_patch, indent=2) + "\n", encoding="utf-8")
    patch_path.chmod(0o600)
    return payload, token, patch_path


class _ArtifactServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], artifact: Path, route: str, feeder_ip: str):
        self.artifact = artifact
        self.route = route
        self.feeder_ip = feeder_ip
        self.downloaded = threading.Event()
        super().__init__(address, _ArtifactHandler)


class _ArtifactHandler(http.server.BaseHTTPRequestHandler):
    server: _ArtifactServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.client_address[0] != self.server.feeder_ip or self.path != self.server.route:
            self.send_error(404)
            return
        size = self.server.artifact.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Connection", "close")
        self.end_headers()
        with self.server.artifact.open("rb") as source:
            while chunk := source.read(64 * 1024):
                self.wfile.write(chunk)
        self.server.downloaded.set()

    def log_message(self, message: str, *args: Any) -> None:
        print("Bootstrap HTTP: " + (message % args))


def _send_provisioning(config: BootstrapConfig, host: str, port: int) -> None:
    frame = build_set_mqtt_server_frame(host, port, config.feeder_mqtt_member_id)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        for _ in range(3):
            udp.sendto(frame, (config.feeder_ip, 17484))
            time.sleep(0.1)


def _write_identity(config: BootstrapConfig, identity: Any) -> Path:
    output = Path(config.output_dir).expanduser().resolve() / "feeder-mqtt-credentials.json"
    document = {
        "client_id": identity.client_id,
        "username": identity.username,
        "password": identity.password.decode("utf-8", errors="strict"),
    }
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    output.chmod(0o600)
    return output


def _probe_state_agent(config: BootstrapConfig, token: str) -> None:
    request = urllib.request.Request(
        f"http://{config.feeder_ip}:8765/health",
        headers={"Authorization": f"Bearer {token}", "Connection": "close"},
    )
    try:
        # Never forward the State Agent bearer token through an ambient HTTP
        # proxy. The configured endpoint is a direct feeder LAN address.
        with DIRECT_URL_OPENER.open(request, timeout=5) as response:
            document = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as err:
        raise RuntimeError(f"State Agent health check failed: {err}") from err
    if response.status != 200 or document.get("ok") is not True:
        raise RuntimeError("State Agent health check did not return ok=true")


def _select_target_version(config: BootstrapConfig, detected: str | None) -> str:
    if detected is None or not re.fullmatch(r"3\.[0-9]+\.[0-9]+", detected):
        raise RuntimeError("the feeder did not publish a valid 3.x DEVICE_START_EVENT version")
    return config.target_software_version or detected


def run(config: BootstrapConfig) -> None:
    payload, token, options_patch = prepare(config)
    artifact_md5 = hashlib.md5(payload.read_bytes()).hexdigest()  # OEM protocol requirement
    route = "/" + secrets.token_urlsafe(18)
    artifact_url = f"http://{config.bootstrap_host_ip}:{config.bootstrap_http_port}{route}"

    mqtt = BootstrapMqttServer(
        (config.bootstrap_host_ip, config.bootstrap_mqtt_port),
        expected_feeder_ip=config.feeder_ip,
    )
    httpd = _ArtifactServer(
        (config.bootstrap_host_ip, config.bootstrap_http_port), payload, route, config.feeder_ip
    )
    mqtt_thread = threading.Thread(target=mqtt.serve_forever, daemon=True)
    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    mqtt_thread.start()
    http_thread.start()
    try:
        print("Redirecting the feeder to the temporary bootstrap broker with OEM command 0x02.")
        _send_provisioning(config, config.bootstrap_host_ip, config.bootstrap_mqtt_port)
        print("If it does not connect promptly, reboot the feeder once; do not factory-reset it.")
        wait_for(mqtt.identity_ready, 180, "the feeder MQTT CONNECT")
        assert mqtt.identity is not None
        identity_path = _write_identity(config, mqtt.identity)
        print(f"Captured credentials in protected file {identity_path}")
        wait_for(mqtt.device_start_ready, 180, "the feeder DEVICE_START_EVENT")
        target_version = _select_target_version(config, mqtt.software_version)
        print(f"Observed stock firmware {mqtt.software_version}; OTA family value is {target_version}.")
        try:
            probe_mqtt_credentials(
                config.feeder_mqtt_host,
                config.feeder_mqtt_port,
                mqtt.identity,
            )
        except (OSError, PermissionError) as err:
            raise RuntimeError(
                "The final broker does not yet accept the feeder's captured factory username/password. "
                f"Create that exact broker account using {identity_path}, then rerun bootstrap. ({err})"
            ) from err
        print("Final broker accepted the feeder credentials.")

        wait_for(mqtt.ota_subscription_ready, 60, "the feeder OTA subscription")
        ota_payload = build_ota_command(
            msg_id="bootstrap-" + secrets.token_hex(8),
            artifact_url=artifact_url,
            artifact_md5=artifact_md5,
            target_version=target_version,
        )
        topic = mqtt.publish_ota(ota_payload)
        print(f"Published correlated OTA bootstrap command on {topic}")
        wait_for(mqtt.ota_publish_acked, 30, "the feeder MQTT OTA PUBACK")
        wait_for(httpd.downloaded, 60, "the feeder payload download")
        wait_for(mqtt.ota_result, 180, "OEM OTA terminal report")
        if mqtt.ota_error is not None:
            raise RuntimeError(f"OEM rejected or failed the OTA bootstrap: {mqtt.ota_error}")
        print("OEM accepted the payload. Waiting for the production OTA1 startup event.")
        wait_for(
            mqtt.post_ota_start_ready,
            240,
            "restored OEM firmware to publish DEVICE_START_EVENT after bootstrap",
        )

        _send_provisioning(config, config.feeder_mqtt_host, config.feeder_mqtt_port)
        print("Restored the OEM MQTT endpoint to the final broker.")
        if config.bootstrap_host_ip == config.home_assistant_ip:
            deadline = time.monotonic() + 60
            while True:
                try:
                    _probe_state_agent(config, token)
                    print("Authenticated State Agent health check passed.")
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(2)
        else:
            print(
                "Skipped the State Agent HTTP check because this setup host is not the configured "
                "Home Assistant allowlisted IP. Verify /health from Home Assistant."
            )
        complete = Path(config.output_dir).expanduser().resolve() / "BOOTSTRAP_COMPLETE"
        complete.write_text(
            "Bootstrap payload installed; OEM endpoint restoration sent.\n"
            f"Apply the generated add-on options from {options_patch}.\n",
            encoding="utf-8",
        )
        complete.chmod(0o600)
        print(f"Bootstrap transaction complete. Apply {options_patch} in the add-on configuration.")
    finally:
        mqtt.shutdown()
        httpd.shutdown()
        mqtt.server_close()
        httpd.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure_parser = subparsers.add_parser("configure", help="interactively create a protected config")
    configure_parser.add_argument("--output", type=Path, default=Path("bootstrap-config.json"))
    command_help = {
        "prepare": "validate inputs and build the payload without contacting the feeder",
        "run": "perform the guarded two-pass feeder bootstrap transaction",
    }
    for command in ("prepare", "run"):
        child = subparsers.add_parser(command, help=command_help[command])
        child.add_argument("--config", type=Path, required=True)
        if command == "run":
            child.add_argument(
                "--yes",
                action="store_true",
                help="confirm the recoverability warning and perform the OEM OTA transaction",
            )
    args = parser.parse_args()
    if args.command == "configure":
        configure(args.output.resolve())
        return 0
    config = BootstrapConfig.load(args.config.resolve())
    if args.command == "prepare":
        payload, _, patch = prepare(config)
        print(f"Prepared {payload}")
        print(f"Prepared protected add-on options patch {patch}")
        return 0
    if not args.yes:
        answer = input(
            "This experimental operation changes feeder startup and both OTA slots. "
            "Continue only on a recoverable lab-tested path [y/N]? "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled without sending provisioning or OTA commands.")
            return 2
    run(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
