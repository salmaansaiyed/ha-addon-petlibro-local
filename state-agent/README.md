# PLAF203 State Agent

The State Agent is the small feeder-resident service that gives the Home
Assistant controller authenticated access to persistent feeder truth. It
decodes a fixed allowlist of OEM state files and supports a tightly constrained
signed self-update transaction.

It is deliberately **not** a remote shell or general filesystem API. It never
accepts caller-selected paths or commands and does not expose MQTT, camera, or
TUTK credentials.

## Responsibilities and boundaries

The agent:

- reads known files below `/user/data` into one immutable request snapshot;
- validates exact file sizes before decoding;
- returns semantic values, optional raw bytes, and stable revisions;
- classifies decoded fields as `persistent`, `effective_cached`, or `runtime`;
- exposes pending firmware feed events as a queue, not durable history; and
- stages signed agent updates for activation by its runit supervisor.

Only `persistent` fields may verify a user command. Firmware-calculated cache
fields and runtime bytes can be stale on disk. In particular,
`/v1/core.motor_state_raw` is informational and is not used to reconstruct
current dispensing status; that uses solicited live firmware telemetry in the
Home Assistant controller.

## API

The normal API listens on TCP port `8765` and requires both a bearer token and
a configured source-IP allowlist. A disallowed peer is closed immediately
after `accept`, before its HTTP request is parsed or it can occupy the single
request-processing slot. Bearer authentication remains a second boundary for
allowed peers. Principal routes are:

- `GET /health`: process and source-file health;
- `GET /v1/rev`: persistent snapshot revisions;
- `GET /v1/core`: decoded core state and complete feeding-plan collection
  (`?raw=1` adds diagnostic bytes);
- `GET /v1/feed-events`: pending outbound firmware event slots (`?raw=1` adds
  bounded raw slot bytes);
- `GET /v1/version`: installed agent/API/platform metadata;
- `GET /v1/update-status`: durable update transaction state;
- `POST /v1/update`: fixed-format signed update upload.

`state.bin` must be exactly 236 bytes. Feeding plans are decoded as exact
47-byte records; schedule equality excludes runtime execution and regenerated
sync metadata. `feed_rec.bin` is a ring of pending 93-byte event slots and must
not be presented as permanent feed history.

## Installation

The recommended installation path is the repository's
[unified no-UART installer](../installer/README.md). It packages the ARM
binaries, runit services, token, source-IP ACL, and minimal startup integration
into the one-time OEM OTA bootstrap.

For authorized shell-based development, install these artifacts under
`/user/data/local-state-agent`:

- `plaf203-state-agent`;
- `plaf203-update-fs`;
- `runit/`;
- a mode-0600 `token`; and
- the startup integration in [`app_start_snippet.sh`](app_start_snippet.sh).

The run command must set `--allow-ip` to the Home Assistant host that originates
API requests. The runtime also requires the feeder's fixed
`/usr/bin/flock`, `/usr/bin/nc`, and `/usr/bin/sv` tools.

## Build and test

Host build:

```bash
make -C state-agent clean all
```

Static ARMv7 hard-float release build:

```bash
make -C state-agent clean arm-release
```

The cross-build requires `arm-linux-gnueabihf-gcc` and emits
`plaf203-state-agent` plus the narrow durability helper
`plaf203-update-fs`. Both outputs and `build/` are ignored; do not commit local
binaries.

Tests run the host binary against synthetic feeder files:

```bash
python3 -m pytest state-agent/tests -q
```

The agent embeds the version from [`VERSION`](VERSION), the Ed25519 candidate
trust anchor from [`release-public-key.hex`](release-public-key.hex), and the
vendored audited crypto implementation in [`vendor/monocypher`](vendor/monocypher).

## Signed updates

The add-on downloads an immutable artifact and signed manifest over HTTPS,
verifies them, then uploads one fixed binary frame to `POST /v1/update`. The
agent verifies the detached Ed25519 signature, strict manifest schema,
platform/API compatibility, SemVer progression, artifact size/SHA-256, and a
basic ARMv7 ELF shape before staging anything.

The runit supervisor—not the HTTP process—owns activation:

1. serialize with `update/transaction.lock`;
2. stop the old service;
3. durably create one rolling backup;
4. activate the candidate through same-directory atomic replacement;
5. start and probe authenticated `/health` and exact `/v1/version`;
6. commit success or durably restore the validated backup.

Transaction phases are persisted so reboot recovery can finish a proven
candidate or resume rollback. The fixed-path `plaf203-update-fs` helper performs
file/directory fsync around transaction-significant replacement and cleanup.

Create a normal release manifest with:

```bash
python3 state-agent/scripts/build_release_manifest.py \
  --artifact-path state-agent/plaf203-state-agent \
  --artifact-url https://updates.example.invalid/plaf203/VERSION/plaf203-state-agent \
  --release-url https://example.invalid/releases/VERSION \
  --signing-key /secure/path/ed25519-private.pem \
  --public-key-file state-agent/release-public-key.hex \
  --output-dir /secure/output/release
```

Private signing keys must never enter this repository. Normal releases require
the signer-derived public key to match the embedded candidate key. Intentional
key rotation uses `--rotate-trust-anchor`: the current private key signs a
candidate that embeds the next public key. Deploy and verify that transition
before changing the add-on's trusted key material.

## Read-only verification

From an allowed host:

```bash
curl -fsS -H "Authorization: Bearer $TOKEN" http://FEEDER_IP:8765/health
curl -fsS -H "Authorization: Bearer $TOKEN" http://FEEDER_IP:8765/v1/rev
curl -fsS -H "Authorization: Bearer $TOKEN" http://FEEDER_IP:8765/v1/core
```

Use `?raw=1` only for bounded local diagnostics. Never paste raw responses or
tokens into public reports.
