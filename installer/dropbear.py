"""Pinned, reproducible-enough Dropbear source acquisition and ARM build."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path


DROPBEAR_REPOSITORY = "https://github.com/mkj/dropbear.git"
DROPBEAR_TAG = "DROPBEAR_2026.94"
DROPBEAR_COMMIT = "28216cd9af822732a1549b78621c2ea9a76a0fe5"
ZIG_VERSION = "0.16.0"
ZIG_RELEASES = {
    "x86_64": (
        "https://ziglang.org/download/0.16.0/zig-x86_64-linux-0.16.0.tar.xz",
        "70e49664a74374b48b51e6f3fdfbf437f6395d42509050588bd49abe52ba3d00",
    ),
    "aarch64": (
        "https://ziglang.org/download/0.16.0/zig-aarch64-linux-0.16.0.tar.xz",
        "ea4b09bfb22ec6f6c6ceac57ab63efb6b46e17ab08d21f69f3a48b38e1534f17",
    ),
}
BUILD_SCHEMA = 1


def default_cache_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "build" / "bootstrap" / "cache"


def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _run_logged(
    command: list[str], *, cwd: Path, env: dict[str, str], log_path: Path
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
        raise RuntimeError(
            f"Dropbear build command failed; log: {log_path}\n" + "\n".join(tail)
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path, expected_sha256: str) -> None:
    if destination.is_file() and _sha256(destination) == expected_sha256:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".new")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "plaf203-bootstrap/1"})
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
        if response.status != 200:
            raise RuntimeError(f"download failed with HTTP {response.status}: {url}")
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    actual = _sha256(temporary)
    if actual != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {url}: expected {expected_sha256}, got {actual}")
    temporary.replace(destination)


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:xz") as source:
        # Python's data filter rejects absolute paths, traversal, devices, and
        # other archive features that are inappropriate for a build tool.
        source.extractall(destination, filter="data")


def _zig(cache: Path) -> Path:
    machine = platform.machine().lower()
    aliases = {"amd64": "x86_64", "arm64": "aarch64"}
    machine = aliases.get(machine, machine)
    if platform.system() != "Linux" or machine not in ZIG_RELEASES:
        raise RuntimeError("automatic Dropbear builds support Linux x86_64 and aarch64 hosts")
    url, checksum = ZIG_RELEASES[machine]
    toolchain_root = cache / "toolchains"
    extracted = toolchain_root / f"zig-{machine}-linux-{ZIG_VERSION}"
    executable = extracted / "zig"
    if executable.is_file():
        return executable
    archive = toolchain_root / Path(url).name
    _download(url, archive, checksum)
    with tempfile.TemporaryDirectory(prefix="zig-extract-", dir=toolchain_root) as raw:
        temporary = Path(raw)
        _safe_extract_tar(archive, temporary)
        candidate = temporary / extracted.name
        if not (candidate / "zig").is_file():
            raise RuntimeError("verified Zig archive did not contain the expected executable")
        candidate.replace(extracted)
    return executable


def _dropbear_source(cache: Path) -> Path:
    source = cache / "sources" / f"dropbear-{DROPBEAR_COMMIT}"
    if source.is_dir():
        head = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        origin = subprocess.run(
            ["git", "-C", str(source), "remote", "get-url", "origin"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        status = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if (
            head.returncode == 0
            and head.stdout.strip() == DROPBEAR_COMMIT
            and origin.returncode == 0
            and origin.stdout.strip() == DROPBEAR_REPOSITORY
            and status.returncode == 0
            and not status.stdout.strip()
        ):
            return source
        shutil.rmtree(source)
    source.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dropbear-clone-", dir=source.parent) as raw:
        temporary = Path(raw) / "source"
        _run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                DROPBEAR_TAG,
                "--single-branch",
                "--quiet",
                DROPBEAR_REPOSITORY,
                str(temporary),
            ]
        )
        resolved = subprocess.check_output(
            ["git", "-C", str(temporary), "rev-parse", "HEAD"], text=True
        ).strip()
        if resolved != DROPBEAR_COMMIT:
            raise RuntimeError(
                f"{DROPBEAR_TAG} resolved to unexpected commit {resolved}; refusing to build"
            )
        temporary.replace(source)
    return source


def _validate_bundle(bundle: Path) -> bool:
    binary = bundle / "dropbearmulti"
    license_file = bundle / "LICENSE.dropbear"
    build_info = bundle / "BUILD_INFO.dropbear.json"
    if not binary.is_file() or not license_file.is_file() or not build_info.is_file():
        return False
    try:
        manifest = json.loads(build_info.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    expected = {
        "schema": BUILD_SCHEMA,
        "repository": DROPBEAR_REPOSITORY,
        "tag": DROPBEAR_TAG,
        "commit": DROPBEAR_COMMIT,
        "zig_version": ZIG_VERSION,
        "target": "arm-linux-musleabihf",
        "cpu": "cortex_a7",
        "programs": ["dropbear", "dropbearkey"],
        "password_auth": False,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        return False
    if manifest.get("dropbearmulti_sha256") != _sha256(binary):
        return False
    header = binary.read_bytes()[:52]
    if len(header) < 52 or header[:5] != b"\x7fELF\x01":
        return False
    if int.from_bytes(header[18:20], "little") != 40:
        return False
    # EF_ARM_ABI_FLOAT_HARD must be present. Dynamic builds would also contain
    # an interpreter string; musl itself can contain its loader name, so use
    # readelf during the build for authoritative linkage validation.
    return bool(int.from_bytes(header[36:40], "little") & 0x400)


def build_dropbear(cache_dir: Path | None = None) -> Path:
    """Return a verified cached static ARM hard-float Dropbear bundle."""

    cache = (cache_dir or default_cache_dir()).expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    cache.chmod(0o700)
    bundle = cache / "builds" / f"dropbear-{DROPBEAR_COMMIT}-armv7-musl"
    if _validate_bundle(bundle):
        return bundle

    for command in ("git", "make", "readelf"):
        if shutil.which(command) is None:
            raise RuntimeError(f"required build command is unavailable: {command}")
    zig = _zig(cache)
    source = _dropbear_source(cache)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    log_path = cache.parent / "dropbear-build.log"
    log_path.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="dropbear-build-", dir=cache) as raw:
        build = Path(raw) / "source"
        shutil.copytree(source, build, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        wrapper = Path(raw) / "arm-linux-musleabihf-gcc"
        wrapper.write_text(
            "#!/bin/sh\n"
            f'exec "{zig}" cc -target arm-linux-musleabihf -mcpu=cortex_a7 "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        (build / "localoptions.h").write_text(
            "#define DROPBEAR_SVR_PASSWORD_AUTH 0\n"
            "#define DROPBEAR_SVR_PAM_AUTH 0\n"
            "#define DROPBEAR_SVR_PUBKEY_AUTH 1\n"
            "#define DROPBEAR_CLI_PASSWORD_AUTH 0\n"
            "#define DROPBEAR_X11FWD 0\n",
            encoding="ascii",
        )
        env = os.environ.copy()
        zig_cache = cache / "zig-cache"
        temporary_files = cache / "tmp"
        zig_cache.mkdir(exist_ok=True)
        temporary_files.mkdir(exist_ok=True)
        env.update(
            {
                "CC": str(wrapper),
                "AR": f"{zig} ar",
                "RANLIB": f"{zig} ranlib",
                "SOURCE_DATE_EPOCH": "0",
                "TMPDIR": str(temporary_files),
                "ZIG_GLOBAL_CACHE_DIR": str(zig_cache / "global"),
                "ZIG_LOCAL_CACHE_DIR": str(zig_cache / "local"),
            }
        )
        build_triplet = (
            "x86_64-pc-linux-gnu"
            if platform.machine().lower() in {"x86_64", "amd64"}
            else "aarch64-unknown-linux-gnu"
        )
        _run_logged(
            [
                "./configure",
                f"--build={build_triplet}",
                "--host=arm-linux-musleabihf",
                "--disable-harden",
                "--disable-zlib",
                "--disable-lastlog",
                "--disable-utmp",
                "--disable-utmpx",
                "--disable-wtmp",
                "--disable-wtmpx",
                "--disable-syslog",
            ],
            cwd=build,
            env=env,
            log_path=log_path,
        )
        _run_logged(
            [
                "make",
                "PROGRAMS=dropbear dropbearkey",
                "MULTI=1",
                "STATIC=1",
                "CFLAGS=-Os -g0 -fstack-protector-strong -D_FORTIFY_SOURCE=2 "
                + f"-I{build} -I{build / 'src'} -ffile-prefix-map={build}=.",
                "LDFLAGS=-s -Wl,--build-id=none",
            ],
            cwd=build,
            env=env,
            log_path=log_path,
        )
        binary = build / "dropbearmulti"
        attributes = subprocess.check_output(["readelf", "-h", "-A", str(binary)], text=True)
        program_headers = subprocess.check_output(["readelf", "-l", str(binary)], text=True)
        dynamic = subprocess.run(
            ["readelf", "-d", str(binary)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        ).stdout
        if not all(
            marker in attributes
            for marker in (
                "ELF32",
                "ARM",
                "hard-float ABI",
                "Tag_CPU_arch: v7",
                "Tag_ABI_VFP_args: VFP registers",
            )
        ):
            raise RuntimeError("Dropbear build is not ARMv7 hard-float ELF32")
        if "INTERP" in program_headers or "NEEDED" in dynamic:
            raise RuntimeError("Dropbear build is dynamically linked")
        candidate = Path(raw) / "bundle"
        candidate.mkdir()
        output_binary = candidate / "dropbearmulti"
        shutil.copyfile(binary, output_binary)
        shutil.copyfile(build / "LICENSE", candidate / "LICENSE.dropbear")
        build_info = {
            "schema": BUILD_SCHEMA,
            "repository": DROPBEAR_REPOSITORY,
            "tag": DROPBEAR_TAG,
            "commit": DROPBEAR_COMMIT,
            "zig_version": ZIG_VERSION,
            "target": "arm-linux-musleabihf",
            "cpu": "cortex_a7",
            "programs": ["dropbear", "dropbearkey"],
            "password_auth": False,
            "dropbearmulti_sha256": _sha256(output_binary),
        }
        (candidate / "BUILD_INFO.dropbear.json").write_text(
            json.dumps(build_info, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        output_binary.chmod(0o700)
        (candidate / "LICENSE.dropbear").chmod(0o600)
        (candidate / "BUILD_INFO.dropbear.json").chmod(0o600)
        if bundle.exists():
            shutil.rmtree(bundle)
        candidate.replace(bundle)
    if not _validate_bundle(bundle):
        raise RuntimeError("built Dropbear bundle failed final validation")
    return bundle
