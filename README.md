# Petlibro Local

Petlibro Local brings supported Petlibro camera feeders onto a user-controlled
LAN. It replaces the feeder's cloud MQTT connection with a local broker,
publishes feeder controls and observations to Home Assistant, exposes the camera
through go2rtc, and reads durable feeder configuration from a small on-device
State Agent.

The project currently targets the MQTT-based **PLAF203 / Granary Camera
Feeder** family. It is independent community software and is not affiliated
with Petlibro.

## Components

| Component | Responsibility |
| --- | --- |
| [`addon/`](addon/) | Home Assistant add-on image, configuration renderer, AppDaemon MQTT controller, and patched go2rtc camera backend |
| [`state-agent/`](state-agent/) | Authenticated feeder-resident service that decodes persistent state and stages signed State Agent updates |
| [`installer/`](installer/) | Guided no-UART bootstrap that migrates an already claimed stock feeder to the local architecture |
| [`docker/`](docker/) | Docker Compose deployment for Linux hosts outside Home Assistant OS |

The add-on is the long-running coordinator. The State Agent does not replace the
OEM firmware: it runs beside it and exposes only narrowly defined local files.
The installer is a one-time provisioning tool; after bootstrap, State Agent
updates use the signed update path managed by the add-on.

```text
Home Assistant ─┐
                ├─ MQTT broker ───── OEM feeder firmware
Petlibro add-on ┘        │                    │
       │                 └─ feeder control   ├─ local state files
       ├─ AppDaemon MQTT controller          └─ State Agent HTTP API
       └─ patched go2rtc ───── LAN camera transport
```

See [Architecture](docs/architecture.md) for the trust and state-ownership
boundaries.

## Supported functionality

- Home Assistant MQTT discovery for feeder settings, schedules, diagnostics,
  feeding controls, and dispensing observations.
- Fresh dispensing-state reconstruction after add-on or Home Assistant restart.
- Persistent setting and feeding-plan verification against feeder-local state.
- PLAF203 H.264 SD/HD camera streaming through RTSP, WebRTC, and the go2rtc web
  interface; optional AAC is supported by the camera backend.
- Automatic feeder serial, camera UID, and LAN-address discovery.
- Signed, rollback-capable State Agent updates after initial bootstrap.
- Optional key-only Dropbear installation during bootstrap.

Current release constraints:

- Home Assistant add-on image: `amd64`.
- Tested feeder family: PLAF203; firmware variants may differ.
- The add-on remains marked `experimental` while hardware coverage and the
  clean-stock bootstrap workflow receive broader validation.

## Requirements

- A PLAF203 already onboarded to Wi-Fi and claimed in the Petlibro app.
- Home Assistant with an MQTT broker and MQTT integration, or a supported Linux
  host for the Docker deployment.
- A trusted LAN on which the feeder can reach the broker, Home Assistant, and
  the temporary installer host.
- A dedicated broker account for the backend. The installer captures the
  feeder's separate factory MQTT identity so it can also be authorized.
- A Linux setup machine for initial no-UART bootstrap.

The feeder uses plaintext MQTT on tested firmware. Keep it on a trusted or
isolated network and do not expose the broker, State Agent, go2rtc, or SSH
listeners directly to the Internet.

## Recommended installation

1. Add `https://github.com/tannerln7/ha-addon-petlibro-local` to the Home
   Assistant app/add-on repository list and install **Petlibro Local backend**.
2. Create a broker account for the add-on and enter it in the add-on
   configuration. Leave the add-on stopped until bootstrap is ready.
3. On a trusted Linux machine, build the ARM State Agent and run the guided
   installer:

   ```bash
   make -C state-agent arm-release
   python3 installer/bootstrap.py configure --output bootstrap-config.json
   python3 installer/bootstrap.py prepare --config bootstrap-config.json
   python3 installer/bootstrap.py run --config bootstrap-config.json
   ```

4. The first `run` may stop after capturing the feeder's factory MQTT
   credentials. Add that account and its topic ACL to the local broker, then run
   the same command again.
5. Merge the generated `addon-options.patch.json` into the add-on configuration,
   start the add-on, and verify State Agent reconciliation and camera discovery.

This is the intended installation architecture, but the complete production
payload has not yet been fault-injected on a clean stock feeder across all
supported firmware variants. Read the installer's safety and recovery notes
before proceeding. The process intentionally preserves an OEM firmware donor
slot until installation succeeds and restores normal OEM firmware to OTA1.

See the [complete installation guide](docs/installation.md) and the
[installer reference](installer/README.md) before modifying a feeder.

## Runtime endpoints

With default host networking:

| Service | Default endpoint |
| --- | --- |
| go2rtc web/API | `http://HOME_ASSISTANT_HOST:1984/` |
| RTSP | `rtsp://HOME_ASSISTANT_HOST:8554/petlibro_plaf203_<serial>` |
| WebRTC | TCP and UDP port `8555` |
| State Agent | `http://FEEDER_IP:8765/` (bearer token and source-IP restricted) |

Camera sessions are lazy and begin when a consumer opens the generated stream.

## Documentation

### Users and operators

- [Installation](docs/installation.md)
- [Add-on configuration](addon/DOCS.md)
- [Configuration internals and advanced options](docs/configuration.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Docker / LXC deployment](docker/README.md)
- [Release history](addon/CHANGELOG.md)

### Components and interfaces

- [Home Assistant add-on](addon/README.md)
- [State Agent](state-agent/README.md)
- [Installer/bootstrap](installer/README.md)
- [MQTT camera contract](docs/mqtt-camera-contract.md)

### Development

- [Architecture](docs/architecture.md)
- [Development guide](docs/development.md)
- [Contributing](CONTRIBUTING.md)
- [Camera backend development](docs/camera-development.md)
- [Camera diagnostics](docs/camera-debugging.md)
- [Sanitized firmware 3.1.48 MQTT protocol reference](docs/protocol/mqtt-firmware-3.1.48.md)

## Security and privacy

Never commit or publish feeder MQTT credentials, State Agent tokens, private
signing keys, SSH private keys, serials, camera UIDs, raw packet captures, or
decrypted protocol dumps. Generated installer output is mode-restricted and
ignored by Git, but it remains sensitive.

The State Agent update design uses a compiled Ed25519 trust anchor, a detached
signature over exact manifest bytes, artifact hash/size verification, and a
fixed-path feeder-side transaction with rollback. The one-time OEM bootstrap is
a separate trust boundary and should run only on a trusted LAN.

## License

Repository packaging and documentation use the [MIT License](LICENSE). Bundled
go2rtc and the original AppDaemon controller retain the license files in their
component directories.
