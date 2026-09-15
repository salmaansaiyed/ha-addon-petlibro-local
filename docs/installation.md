# Installation

This guide describes the intended end-user installation: install the Home
Assistant add-on, then use the unified no-UART installer to move an already
claimed stock PLAF203 feeder to the local MQTT broker and install the State
Agent.

The installer uses OEM LAN provisioning and OTA behavior. It does not require
UART access or a DNS rewrite during bootstrap. The resulting feeder still runs
the OEM `AF203_FW`; the bootstrap payload is temporary.

> [!CAUTION]
> Arbitrary OTA execution and return to OEM firmware have been demonstrated on
> firmware 3.1.48, and the generated production transaction has automated
> tests. The complete workflow has not yet been power-loss fault-injected on a
> clean stock feeder across all firmware variants. Use a stable power source,
> read the [installer safety notes](../installer/README.md#safety-and-recovery),
> and proceed only on hardware you own and can recover.

## 1. Prepare Home Assistant and MQTT

1. Install and start an MQTT broker, such as the Mosquitto broker add-on.
2. Enable Home Assistant's MQTT integration.
3. Create a dedicated broker account for the Petlibro Local backend.
4. Add this repository to Home Assistant:

   ```text
   https://github.com/tannerln7/ha-addon-petlibro-local
   ```

5. Install **Petlibro Local backend**.
6. Enter the backend MQTT account in the add-on configuration, but leave the
   add-on stopped until bootstrap has generated the remaining values.

The physical feeder has a different factory MQTT identity. The installer
captures it during the first bootstrap pass; do not reuse the backend account
for the feeder.

## 2. Prepare a setup machine

Use a Linux machine on the same trusted LAN as the feeder and Home Assistant.
The feeder must be able to reach:

- UDP port `17484` on the feeder for OEM provisioning;
- the temporary MQTT listener on the setup machine (default TCP `1884`);
- the temporary HTTP listener on the setup machine (default TCP `18080`);
- the final broker and Home Assistant after installation.

Install:

- Python 3.12 or newer;
- GNU Make;
- an `arm-linux-gnueabihf-gcc` cross compiler;
- optional static ARMv7 `dropbear` and `dropbearkey` binaries if SSH is
  desired.

Clone the repository and build the feeder artifacts:

```bash
git clone https://github.com/tannerln7/ha-addon-petlibro-local.git
cd ha-addon-petlibro-local
make -C state-agent arm-release
```

The build must produce static ELF32 ARMv7 hard-float
`state-agent/plaf203-state-agent` and `state-agent/plaf203-update-fs`.

## 3. Create the bootstrap configuration

```bash
python3 installer/bootstrap.py configure --output bootstrap-config.json
```

The guided prompt collects:

- feeder IPv4 address;
- setup-machine, Home Assistant, and final-broker IPv4 addresses;
- backend MQTT host, account, and password;
- optional signed State Agent manifest URL;
- optional SSH public key and Dropbear artifact directory.

Use a final broker address that the feeder can resolve and reach directly.
`core-mosquitto` is valid for the add-on's internal connection but is not a
feeder-reachable address.

Configuration and generated output contain credentials and tokens. They are
written with restrictive permissions and ignored by Git. Keep them local.

## 4. Build and review the payload

```bash
python3 installer/bootstrap.py prepare --config bootstrap-config.json
```

This creates a protected output directory containing:

- the one-time `AF203_FW.bootstrap` payload;
- a generated State Agent bearer token;
- `addon-options.patch.json`;
- later, the captured feeder MQTT credential and completion marker.

Preparation does not contact or modify the feeder. Review the paths, addresses,
and optional SSH key before continuing.

## 5. Run the two-pass broker migration

```bash
python3 installer/bootstrap.py run --config bootstrap-config.json
```

The installer first points the feeder at a temporary local MQTT server. If the
feeder does not reconnect promptly, it asks for one feeder power cycle. The
temporary server captures the feeder's factory MQTT CONNECT identity and probes
the final broker with those exact credentials.

The first run normally stops safely if the final broker does not yet authorize
the feeder:

1. Open the generated `feeder-mqtt-credentials.json`.
2. Add that exact client/user credential to the broker.
3. Restrict it to the feeder's `dl/PLAF203/<serial>/device/#` topic tree.
4. Run the same command again.

No OTA command is sent until the final broker accepts the feeder identity.

On the successful pass, the installer:

1. publishes a correlated OEM OTA command;
2. serves one randomized payload URL only to the configured feeder;
3. waits for download and OTA acknowledgement;
4. observes the post-install OEM startup event;
5. redirects the feeder to the final broker;
6. verifies State Agent health when the setup host is the allowed Home
   Assistant source IP.

The payload preserves a known-good OEM donor before modifying the inactive
slot. It installs the State Agent and optional key-only Dropbear, restores the
temporary slot from OEM firmware, selects OTA1 for production, and reboots.

## 6. Apply the add-on configuration

Merge the generated `addon-options.patch.json` into the add-on's Configuration
tab. It supplies:

- backend broker connection;
- durable feeder-facing broker address;
- State Agent URL and bearer token;
- optional signed State Agent update manifest.

It deliberately does not edit Home Assistant options through an API. Review the
merged values, save, and start the add-on.

## 7. Verify

Check the add-on log for:

1. backend MQTT connection;
2. feeder serial and `DEVICE_START_EVENT`;
3. State Agent `/v1/core` reconciliation;
4. camera UID discovery and LAN address resolution;
5. generated go2rtc stream;
6. State Agent update status, when configured.

Home Assistant should create the feeder entities through MQTT discovery.
Opening the camera starts the lazy camera session. With defaults, use:

```text
http://HOME_ASSISTANT_HOST:1984/
rtsp://HOME_ASSISTANT_HOST:8554/petlibro_plaf203_<serial>
```

The dispensing entity is intentionally non-retained. It is reconstructed from
a fresh solicited firmware `motorState` after startup or Home Assistant birth;
grain-output events drive later transitions. Durable “Last dispense …” values
are retained after they have first been populated by a feed event.

## Updating

- Update the Home Assistant add-on through Home Assistant normally.
- After initial installation, State Agent releases use the signed update
  mechanism exposed by the add-on; do not rerun OEM bootstrap for routine State
  Agent updates.
- Preserve the State Agent token and broker credentials in Home Assistant
  backups.

See [State Agent updates](configuration.md#state-agent-updates) and the
[State Agent release guide](../state-agent/README.md#signed-updates).

## Alternative deployment

For Docker Compose or Debian/Proxmox LXC, use the
[Docker deployment guide](../docker/README.md). The same feeder bootstrap and
broker requirements apply.
