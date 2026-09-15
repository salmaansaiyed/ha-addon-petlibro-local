# PLAF203 installer/bootstrap

This directory contains the unified end-user installation workflow for an
already claimed stock PLAF203 feeder. It prepares and delivers the production
feeder payload without UART access, while generating the matching Home
Assistant add-on configuration.

The installer is the only component that uses the OEM provisioning and
firmware OTA surfaces. Neither the installed State Agent nor the running add-on
retains this capability.

## Installation flow

The guided workflow:

1. sends the OEM LAN provisioning command to point the feeder at a narrowly
   scoped temporary MQTT server;
2. captures the feeder's own plaintext MQTT CONNECT identity during a normal
   reboot/startup and services only the minimum OEM startup messages;
3. verifies that the final user-supplied broker accepts that exact feeder
   identity;
4. publishes a correlated OEM `OTA_UPGRADE` request for a one-time payload;
5. installs the State Agent, runit services, ACL/token, and optional key-only
   Dropbear;
6. restores the payload slot from the untouched OEM slot, selects OTA1, and
   reboots into the original firmware; and
7. redirects the restored feeder to the final MQTT broker and writes an add-on
   options patch.

No DNS rewrite is required by the implemented path. The OEM request supplies
both the payload URL and its MD5; the digest detects transfer corruption but is
not an authenticity boundary.

## Safety and recovery

**The architecture is the intended production installation path, but hardware
validation is not complete enough to call every stock-firmware variant
recoverable.** Arbitrary OTA execution and return to the untouched OEM slot
have been demonstrated on firmware 3.1.48. Host-side payload transactions are
tested, but the complete production payload still needs clean-device and
power-loss fault-injection coverage with a recoverable UART/flash setup.

The payload never modifies both firmware images together. It first validates
the opposite slot as an ELF donor and selects that donor for the next boot.
Only then does it copy the donor through a temporary file over the payload
slot, verify the copy byte-for-byte, install the minimal final startup, and
select OTA1. A handled failure reselects a known-good donor. After a power loss,
rerun the workflow and verify state rather than assuming installation finished.

The production payload intentionally excludes telnet, packet capture, boot
timing tools, live firmware logging, debug mounts, and other research artifacts.

## Prerequisites

- A PLAF203 already onboarded to Wi-Fi and claimed in the OEM app.
- A trusted LAN shared by feeder, setup machine, Home Assistant, and broker.
- A Home Assistant MQTT broker account for the add-on.
- Permission to add the captured factory feeder account to that broker.
- Python 3.12 or newer on the setup machine.
- Static ARMv7 hard-float State Agent release artifacts built with
  `make -C state-agent arm-release`.
- Optional: static ARM Dropbear binaries and one OpenSSH public-key line.

The feeder-facing MQTT port must be four decimal digits (`1000`–`9999`) due to
the OEM endpoint parser. The setup machine must receive feeder UDP/17484 and
the configured temporary MQTT/HTTP ports (defaults: TCP/1884 and TCP/18080).

## Configure and prepare

From the repository root:

```bash
make -C state-agent clean arm-release
python3 installer/bootstrap.py configure --output bootstrap-config.json
python3 installer/bootstrap.py prepare --config bootstrap-config.json
```

`configure` collects feeder/setup/Home Assistant/broker addresses, the add-on
broker account, optional signed State Agent update manifest, and optional SSH
materials. `prepare` validates inputs and builds the exact payload and generated
configuration for review without contacting the feeder.

Generated configuration, captured feeder credentials, bearer tokens, payloads,
and add-on options contain secrets. They are mode 0600 and ignored by Git. Do
not paste them into logs or issues.

## Run the two-pass broker migration

```bash
python3 installer/bootstrap.py run --config bootstrap-config.json
```

The first run may deliberately stop after capturing feeder credentials if the
final broker rejects them. Add the reported factory account and an appropriate
ACL to the broker, then run the same command again. OTA is not sent until the
installer has independently proved that both the add-on account and captured
feeder account can authenticate to the final broker.

Review the interactive warning. Use `--yes` only in controlled automation, and
do not remove feeder power during installation.

On success, merge the generated `addon-options.patch.json` into the installed
add-on's configuration and restart the add-on. The installer does not mutate
Home Assistant options directly.

For a shorter user-oriented walkthrough, see the
[installation guide](../docs/installation.md).

## Verification

Completion requires correlated OTA acknowledgment, an exact payload download
from the configured feeder IP, a new firmware MQTT connection and
`DEVICE_START_EVENT`, final broker redirection, and—when the setup host is the
allowed Home Assistant IP—an authenticated State Agent `/health` result.

If setup and Home Assistant use different source IPs, perform the State Agent
health check from Home Assistant. Then verify in add-on logs that:

- the feeder reconnects to the final broker;
- `/v1/core` reconciliation succeeds;
- Home Assistant discovery and controls appear;
- camera identity and LAN resolution complete; and
- signed update status is healthy when configured.

## Configuration reference

The example schema is [`bootstrap-config.example.json`](bootstrap-config.example.json).
Primary implementation modules are:

- [`bootstrap.py`](bootstrap.py): CLI, validation, orchestration, and evidence;
- [`mqtt_server.py`](mqtt_server.py): source-restricted temporary broker;
- [`protocol.py`](protocol.py): OEM MQTT messages and correlation;
- [`payload.py`](payload.py): deterministic production payload construction.

The temporary MQTT server accepts only the configured feeder source IP and the
minimal MQTT 3.1.1 startup/OTA exchange. Its NTP response disables calibration.
The HTTP server exposes one random exact path only to that feeder. OTA reports
are correlated by `msgId` and local connection generation.

## Development and tests

All device-facing operations must remain explicit and source-restricted. Add
new production files through the payload manifest rather than copying an
ambient development directory. Keep payload generation deterministic and test
failure/recovery before changing slot operations.

```bash
python3 -m pytest installer/tests -q
```

See the [development guide](../docs/development.md#installer-development) for
repository conventions. Never test against a feeder you do not own or have
permission to modify.
