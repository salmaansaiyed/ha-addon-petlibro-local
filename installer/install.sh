#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
VENV="$REPO_ROOT/.venv"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

MISSING_DEPENDENCIES=""
MISSING_PACKAGES=""

add_missing_dependency() {
    MISSING_DEPENDENCIES="${MISSING_DEPENDENCIES}
  - $1"
    MISSING_PACKAGES="${MISSING_PACKAGES} $2"
}

check_build_dependencies() {
    MISSING_DEPENDENCIES=""
    MISSING_PACKAGES=""

    if ! command -v python3 >/dev/null 2>&1; then
        add_missing_dependency "Python 3.12 or newer" "python3 python3-venv"
    elif ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
        add_missing_dependency "Python 3.12 or newer" "python3 python3-venv"
    elif ! python3 -c 'import ensurepip, venv' >/dev/null 2>&1; then
        add_missing_dependency "Python venv and ensurepip modules" "python3-venv"
    fi

    command -v git >/dev/null 2>&1 || add_missing_dependency "Git" "git"
    command -v make >/dev/null 2>&1 || add_missing_dependency "GNU Make" "make"
    command -v readelf >/dev/null 2>&1 || add_missing_dependency "readelf (GNU binutils)" "binutils"
    command -v ssh-keygen >/dev/null 2>&1 || add_missing_dependency "ssh-keygen" "openssh-client"
    command -v arm-linux-gnueabihf-gcc >/dev/null 2>&1 || \
        add_missing_dependency "ARM Linux hard-float cross compiler" "gcc-arm-linux-gnueabihf"
}

print_missing_dependencies() {
    printf 'Missing installer build dependencies:%s\n' "$MISSING_DEPENDENCIES" >&2
}

install_build_dependencies() {
    if ! command -v apt-get >/dev/null 2>&1; then
        printf '%s\n' \
            "Automatic dependency installation currently supports Debian/Ubuntu (apt-get)." \
            "Install the dependencies listed above with your system package manager, then rerun:" >&2
        printf '  %s\n' "$0" >&2
        return 1
    fi

    if [ "$(id -u)" -eq 0 ]; then
        privilege=""
    elif command -v sudo >/dev/null 2>&1; then
        privilege="sudo"
    else
        printf '%s\n' \
            "Installing host packages requires root privileges, but sudo is unavailable." >&2
        return 1
    fi

    printf '%s\n' "Updating package metadata and installing required build packages..."
    # Package names are selected from fixed dependency mappings above.
    # shellcheck disable=SC2086
    $privilege apt-get update
    # shellcheck disable=SC2086
    $privilege apt-get install --yes $MISSING_PACKAGES
}

check_build_dependencies
if [ -n "$MISSING_DEPENDENCIES" ]; then
    print_missing_dependencies
    printf 'Install the missing dependencies now? [y/N] '
    IFS= read -r answer || answer=""
    case "$answer" in
        y|Y|yes|YES|Yes)
            install_build_dependencies || {
                print_missing_dependencies
                exit 1
            }
            check_build_dependencies
            if [ -n "$MISSING_DEPENDENCIES" ]; then
                printf '%s\n' "Dependency installation completed, but preflight still fails." >&2
                print_missing_dependencies
                exit 1
            fi
            ;;
        *)
            printf '%s\n' "Cannot start the installer without the dependencies listed above." >&2
            print_missing_dependencies
            exit 1
            ;;
    esac
fi

if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating installer virtual environment at $VENV"
    python3 -m venv "$VENV"
fi

# shellcheck disable=SC1091
. "$VENV/bin/activate"

python -m pip install \
    --disable-pip-version-check \
    --requirement "$REQUIREMENTS"

exec python "$SCRIPT_DIR/bootstrap.py" "$@"
