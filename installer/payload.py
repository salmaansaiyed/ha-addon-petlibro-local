"""Build the self-contained shell payload executed from the inactive OTA slot."""

from __future__ import annotations

import io
import os
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path


INSTALLER = r'''#!/bin/sh
set -eu

ROOT="${PLAF203_BOOTSTRAP_TEST_ROOT:-/user}"
DATA="$ROOT/data"
STATE_HOME="$DATA/local-state-agent"
BOOTSTRAP_HOME="$DATA/plaf203-bootstrap"
OTA1="$ROOT/ota1/AF203_FW"
OTA2="$ROOT/ota2/AF203_FW"
INDEX="$DATA/ota/index.bin"
JOURNAL="$BOOTSTRAP_HOME/status"
STAGE="$DATA/.plaf203-bootstrap-stage"
PHASE=preflight
FAIL_REASON=

mkdir -p "$BOOTSTRAP_HOME"
printf '%s\n' starting > "$JOURNAL"
sync

if [ -n "${PLAF203_BOOTSTRAP_TEST_ROOT:-}" ]; then
    EXECUTION_SLOT="${PLAF203_BOOTSTRAP_TEST_SLOT:-ota2}"
else
    case "$0" in
        /user/ota1/AF203_FW) EXECUTION_SLOT=ota1 ;;
        /user/ota2/AF203_FW) EXECUTION_SLOT=ota2 ;;
        *)
            printf '%s\n' wrong_execution_slot > "$JOURNAL"
            sync
            exit 70
            ;;
    esac
fi

case "$EXECUTION_SLOT" in
    ota1)
        DONOR="$OTA2"
        TARGET="$OTA1"
        SAFE_FW="$OTA2"
        SAFE_INDEX=1
        ;;
    ota2)
        DONOR="$OTA1"
        TARGET="$OTA2"
        SAFE_FW="$OTA1"
        SAFE_INDEX=0
        ;;
    *)
        printf '%s\n' wrong_execution_slot > "$JOURNAL"
        sync
        exit 70
        ;;
esac

if [ ! -x "$DONOR" ]; then
    printf '%s\n' donor_missing > "$JOURNAL"
    sync
    exit 71
fi

donor_magic="$(dd if="$DONOR" bs=1 count=4 2>/dev/null | od -An -tx1 | tr -d ' \n')"
if [ "$donor_magic" != "7f454c46" ]; then
    printf '%s\n' donor_not_elf > "$JOURNAL"
    sync
    exit 72
fi

write_index() {
    case "$1" in
        0) printf '\000\000\000\000' > "$INDEX.new" ;;
        1) printf '\001\000\000\000' > "$INDEX.new" ;;
        *) return 1 ;;
    esac
    mv "$INDEX.new" "$INDEX"
}

select_safe_donor() {
    cat > "$ROOT/app_start.sh.new" << SAFE_START
#!/bin/sh
$SAFE_FW &
SAFE_START
    chmod 755 "$ROOT/app_start.sh.new"
    mv "$ROOT/app_start.sh.new" "$ROOT/app_start.sh"
    mkdir -p "$DATA/ota"
    write_index "$SAFE_INDEX"
    sync
}

select_production_ota1() {
    cp "$STAGE/app_start.sh" "$ROOT/app_start.sh.new"
    chmod 755 "$ROOT/app_start.sh.new"
    mv "$ROOT/app_start.sh.new" "$ROOT/app_start.sh"
    mkdir -p "$DATA/ota"
    write_index 0
    sync
}

fail_safe() {
    result=$?
    trap - EXIT HUP INT TERM
    set +e
    reason="${FAIL_REASON:-install_failed_${PHASE}_${result}}"
    printf '%s\n' "$reason" > "$JOURNAL"
    select_safe_donor
    sync
    if [ -n "${PLAF203_BOOTSTRAP_TEST_ROOT:-}" ]; then
        exit "${result:-1}"
    fi
    reboot
    sleep 30
    exit "${result:-1}"
}

# From this point forward, every failure selects the untouched OEM donor and a
# startup script that cannot execute the temporary payload again.
trap fail_safe EXIT HUP INT TERM

if [ -z "${PLAF203_BOOTSTRAP_TEST_ROOT:-}" ]; then
    for tool in /usr/bin/flock /usr/bin/nc /usr/bin/sv; do
        if [ ! -x "$tool" ]; then
            FAIL_REASON="required_tool_missing_$(basename "$tool")"
            exit 79
        fi
    done
fi

rm -rf "$STAGE"
mkdir -p "$STAGE"
archive_line="$(awk '/^__PLAF203_BOOTSTRAP_ARCHIVE_BELOW__$/{print NR + 1; exit}' "$0")"
if [ -z "$archive_line" ]; then
    printf '%s\n' embedded_archive_missing > "$JOURNAL"
    sync
    exit 73
fi
tail -n "+$archive_line" "$0" | gzip -dc | tar -xf - -C "$STAGE"

for required in \
    local-state-agent/plaf203-state-agent \
    local-state-agent/plaf203-update-fs \
    local-state-agent/app_start_snippet.sh \
    local-state-agent/token \
    app_start.sh; do
    if [ ! -s "$STAGE/$required" ]; then
        printf '%s\n' "archive_missing_$required" > "$JOURNAL"
        sync
        exit 74
    fi
done

available_kb="$(df -Pk "$DATA" 2>/dev/null | awk 'END {print $4}')"
donor_bytes="$(wc -c < "$DONOR" | tr -d ' ')"
case "$available_kb:$donor_bytes" in
    *[!0-9:]*|:*|*:)
        FAIL_REASON=flash_space_check_failed
        exit 75
        ;;
esac
required_kb=$(( (donor_bytes + 1023) / 1024 + 512 ))
if [ "$available_kb" -lt "$required_kb" ]; then
    FAIL_REASON="insufficient_flash_${available_kb}k_available_${required_kb}k_required"
    exit 76
fi

# OEM OTA rewrites app_start.sh to the inactive slot. Replace it with a minimal
# donor startup and durably select the untouched donor before any mutation. If
# the payload is in OTA1, this temporarily selects OTA2 until OTA1 is restored.
PHASE=selecting_safe_donor
if [ ! -f "$BOOTSTRAP_HOME/app_start.oem" ]; then
    cp "$ROOT/app_start.sh" "$BOOTSTRAP_HOME/app_start.oem"
    chmod 700 "$BOOTSTRAP_HOME/app_start.oem"
fi
select_safe_donor

PHASE=installing_services
printf '%s\n' installing_services > "$JOURNAL"
rm -rf "$STATE_HOME.new"
mv "$STAGE/local-state-agent" "$STATE_HOME.new"
chmod 700 "$STATE_HOME.new/plaf203-state-agent" \
    "$STATE_HOME.new/plaf203-update-fs" \
    "$STATE_HOME.new/app_start_snippet.sh"
chmod 600 "$STATE_HOME.new/token"
find "$STATE_HOME.new/runit" -type f -exec chmod 700 {} \;

if [ -d "$STATE_HOME" ]; then
    rm -rf "$STATE_HOME.pre-bootstrap"
    mv "$STATE_HOME" "$STATE_HOME.pre-bootstrap"
fi
mv "$STATE_HOME.new" "$STATE_HOME"
touch "$DATA/enable_state_agent"

if [ -d "$STAGE/dropbear" ]; then
    rm -rf "$DATA/dropbear.new"
    mv "$STAGE/dropbear" "$DATA/dropbear.new"
    chmod 700 "$DATA/dropbear.new/dropbear" "$DATA/dropbear.new/dropbearkey"
    if [ -d "$DATA/dropbear" ]; then
        rm -rf "$DATA/dropbear.pre-bootstrap"
        mv "$DATA/dropbear" "$DATA/dropbear.pre-bootstrap"
    fi
    mv "$DATA/dropbear.new" "$DATA/dropbear"
fi

if [ -s "$STAGE/authorized_keys" ]; then
    mkdir -p "$DATA/dropbear"
    cp "$STAGE/authorized_keys" "$DATA/dropbear/authorized_keys.new"
    chmod 600 "$DATA/dropbear/authorized_keys.new"
    mv "$DATA/dropbear/authorized_keys.new" "$DATA/dropbear/authorized_keys"
    touch "$DATA/enable_ssh"
else
    rm -f "$DATA/enable_ssh"
fi

PHASE=restoring_payload_slot
printf '%s\n' "restoring_$EXECUTION_SLOT" > "$JOURNAL"
if ! cp "$DONOR" "$TARGET.restore"; then
    FAIL_REASON=payload_slot_restore_copy_failed
    exit 77
fi
if ! cmp "$DONOR" "$TARGET.restore"; then
    FAIL_REASON=payload_slot_restore_verify_failed
    exit 78
fi
chmod 755 "$TARGET.restore"
mv "$TARGET.restore" "$TARGET"
sync

# Both slots now contain OEM firmware. OTA1 is safe regardless of which slot
# held the payload, so make it the fallback before installing final startup.
SAFE_FW="$OTA1"
SAFE_INDEX=0
PHASE=selecting_production_ota1
select_production_ota1

rm -rf "$STATE_HOME.pre-bootstrap" "$DATA/dropbear.pre-bootstrap"
rm -rf "$STAGE"
printf '%s\n' installed_rebooting_to_ota1 > "$JOURNAL"
sync
trap - EXIT HUP INT TERM

if [ -n "${PLAF203_BOOTSTRAP_TEST_ROOT:-}" ]; then
    exit 0
fi
reboot
exit 0
__PLAF203_BOOTSTRAP_ARCHIVE_BELOW__
'''.encode("ascii")


APP_START_TEMPLATE = r'''#!/bin/sh

# Installed by the PLAF203 local bootstrap. Keep production startup minimal.
/bin/sh /user/data/local-state-agent/app_start_snippet.sh

if [ -f /user/data/enable_ssh ] && \
   [ -x /user/data/dropbear/dropbear ] && \
   [ -x /user/data/dropbear/dropbearkey ] && \
   [ -s /user/data/dropbear/authorized_keys ]; then
    mkdir -p /dev/pts /root/.ssh /user/data/dropbear
    grep -q ' /dev/pts ' /proc/mounts 2>/dev/null || \
        mount -t devpts devpts /dev/pts 2>/dev/null || true
    chmod 700 /root /root/.ssh
    cp /user/data/dropbear/authorized_keys /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    if [ ! -s /user/data/dropbear/dropbear_ed25519_host_key ]; then
        /user/data/dropbear/dropbearkey -t ed25519 \
            -f /user/data/dropbear/dropbear_ed25519_host_key
        chmod 600 /user/data/dropbear/dropbear_ed25519_host_key
    fi
    pidof dropbear >/dev/null 2>&1 || \
        /user/data/dropbear/dropbear \
            -r /user/data/dropbear/dropbear_ed25519_host_key \
            -p 2222 -s -P /tmp/dropbear.pid
fi

/user/ota1/AF203_FW &
'''


def _copy_required(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    shutil.copyfile(source, destination)


def _validate_public_key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    fields = normalized.split()
    if len(fields) < 2 or fields[0] not in {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
    }:
        raise ValueError("SSH public key must be one supported OpenSSH public-key line")
    if "\n" in normalized or "\r" in normalized:
        raise ValueError("SSH public key must contain exactly one line")
    return normalized


def _validate_dropbear_bundle(path: Path) -> None:
    for name in ("dropbear", "dropbearkey"):
        candidate = path / name
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        header = candidate.read_bytes()[:20]
        if (
            len(header) < 20
            or header[:4] != b"\x7fELF"
            or header[4] != 1
            or int.from_bytes(header[18:20], "little") != 40
        ):
            raise ValueError(f"{candidate} is not an ELF32 ARM executable")


def build_payload(
    *,
    output: Path,
    state_agent_dir: Path,
    home_assistant_ip: str,
    state_agent_token: str,
    ssh_public_key: str = "",
    dropbear_dir: Path | None = None,
) -> Path:
    """Create an OTA payload without persisting intermediate secret files."""

    key = _validate_public_key(ssh_public_key)
    if key and dropbear_dir is None:
        raise ValueError("dropbear_dir is required when SSH is enabled")
    if dropbear_dir is not None:
        _validate_dropbear_bundle(dropbear_dir)
    if not state_agent_token or "\n" in state_agent_token or "\r" in state_agent_token:
        raise ValueError("state-agent token must be one non-empty line")

    with tempfile.TemporaryDirectory(prefix="plaf203-bootstrap-") as raw_temp:
        root = Path(raw_temp)
        agent = root / "local-state-agent"
        agent.mkdir()
        for name in (
            "plaf203-state-agent",
            "plaf203-update-fs",
            "app_start_snippet.sh",
        ):
            _copy_required(state_agent_dir / name, agent / name)
        shutil.copytree(state_agent_dir / "runit", agent / "runit")
        run_path = agent / "runit" / "plaf203-state-agent" / "run"
        run_text = run_path.read_text(encoding="utf-8")
        template_allowlist = "--allow-ip 192.0.2.10"
        configured_allowlist = f"--allow-ip {home_assistant_ip}"
        if template_allowlist not in run_text:
            raise ValueError("State Agent run template does not contain the expected allowlist")
        run_text = run_text.replace(template_allowlist, configured_allowlist)
        if configured_allowlist not in run_text or (
            configured_allowlist != template_allowlist and template_allowlist in run_text
        ):
            raise ValueError("failed to specialize State Agent allowlist")
        run_path.write_text(run_text, encoding="utf-8")
        (agent / "token").write_text(state_agent_token + "\n", encoding="ascii")
        (root / "app_start.sh").write_text(APP_START_TEMPLATE, encoding="ascii")
        (root / "authorized_keys").write_text((key + "\n") if key else "", encoding="ascii")

        if dropbear_dir is not None:
            target = root / "dropbear"
            target.mkdir()
            for name in ("dropbear", "dropbearkey"):
                _copy_required(dropbear_dir / name, target / name)

        for path in root.rglob("*"):
            if path.is_file():
                path.chmod(0o600)

        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz", compresslevel=9) as tar:
            for path in sorted(root.rglob("*")):
                info = tar.gettarinfo(str(path), arcname=str(path.relative_to(root)))
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = 0
                if path.is_file():
                    with path.open("rb") as source:
                        tar.addfile(info, source)
                else:
                    tar.addfile(info)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".new")
    with temporary.open("wb") as stream:
        stream.write(INSTALLER)
        stream.write(archive.getvalue())
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    temporary.replace(output)
    return output
