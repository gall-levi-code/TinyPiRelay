#!/bin/sh
set -eu

# This script is commonly run through sudo. Never resolve tools from the
# caller's PATH, and run the archive inspector without the caller's Python path.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

usage() {
    echo "usage: bootstrap.sh VERSION HTTPS_ARTIFACT_URL SHA256 [INSTALLER_ARGS...]" >&2
    exit 2
}

[ "$#" -ge 3 ] || usage
version=$1
artifact_url=$2
expected_sha256=$3
shift 3
[ "$#" -gt 0 ] || set -- install --apply

case "$artifact_url" in
    https://*) ;;
    *) echo "bootstrap refused a non-HTTPS artifact URL" >&2; exit 2 ;;
esac
case "$expected_sha256" in
    *[!0-9A-Fa-f]*|'') echo "bootstrap requires a 64-character SHA-256" >&2; exit 2 ;;
esac
[ "${#expected_sha256}" -eq 64 ] || {
    echo "bootstrap requires a 64-character SHA-256" >&2
    exit 2
}

# Only an explicit applying invocation may install bootstrap tools. A plan
# stays read-only even on an incomplete image. The full installer checks the
# board, architecture and package transaction before installing media tools.
missing_packages=
command -v curl >/dev/null 2>&1 || missing_packages="$missing_packages curl"
command -v sha256sum >/dev/null 2>&1 || missing_packages="$missing_packages coreutils"
[ -x /usr/bin/python3 ] || missing_packages="$missing_packages python3"
[ -s /etc/ssl/certs/ca-certificates.crt ] || missing_packages="$missing_packages ca-certificates"
if [ -n "$missing_packages" ]; then
    applying=no
    case "$1" in
        install|upgrade)
            for argument do
                [ "$argument" != --apply ] || applying=yes
            done
            ;;
    esac
    if [ "$applying" != yes ] || [ "$(id -u)" != 0 ]; then
        echo "Missing bootstrap packages:$missing_packages. Run the applying installer with sudo to install them automatically." >&2
        exit 2
    fi
    if ! grep -Eq '^VERSION_CODENAME="?(bookworm|trixie)"?$' /etc/os-release ||
       ! { grep -Eq '^ID="?raspbian"?$' /etc/os-release ||
           grep -qi 'Raspberry Pi' /etc/rpi-issue 2>/dev/null; }; then
        echo "Automatic bootstrap prerequisites require Raspberry Pi OS Bookworm or Trixie." >&2
        exit 2
    fi
    echo "Installing bootstrap prerequisites:$missing_packages"
    apt-get -o APT::Update::Error-Mode=any -o Acquire::Retries=2 \
        -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update
    # The package names above are fixed literals, never caller input.
    DEBIAN_FRONTEND=noninteractive apt-get --yes --no-remove --no-upgrade \
        --no-install-recommends install $missing_packages
fi

umask 077
work_dir=$(mktemp -d "/tmp/tinypirelay-bootstrap.XXXXXX")
trap 'rm -rf -- "$work_dir"' EXIT HUP INT TERM
archive=$work_dir/source.tar.gz
extract_dir=$work_dir/source

curl --disable --fail --silent --show-error --location \
    --proto '=https' --proto-redir '=https' --max-filesize 52428800 \
    --connect-timeout 20 --max-time 300 --retry 2 \
    --output "$archive" "$artifact_url"
printf '%s  %s\n' "$expected_sha256" "$archive" | sha256sum -c -s -

source_root=$(/usr/bin/python3 -I - "$archive" "$extract_dir" "$version" <<'PY'
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile

archive = Path(sys.argv[1])
destination = Path(sys.argv[2])
version = sys.argv[3]
if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version) is None:
    raise SystemExit("bootstrap received an invalid version")

expected_root = f"tinypirelay-{version}"
total_size = 0
with tarfile.open(archive, "r:gz") as bundle:
    member_count = 0
    for member in bundle:
        member_count += 1
        if member_count > 10_000:
            raise SystemExit("artifact has an invalid member count")
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or not path.parts
            or path.parts[0] != expected_root
            or ".." in path.parts
            or member.issym()
            or member.islnk()
            or member.isdev()
            or not (member.isfile() or member.isdir())
        ):
            raise SystemExit("artifact contains an unsafe member")
        if member.isfile():
            total_size += member.size
            if total_size > 100 * 1024 * 1024:
                raise SystemExit("artifact expands beyond the size limit")
    if not member_count:
        raise SystemExit("artifact has an invalid member count")
    destination.mkdir(mode=0o700)
    bundle.extractall(destination)

root = destination / expected_root
version_file = root / "VERSION"
installer = root / "install.sh"
if not root.is_dir() or not version_file.is_file() or not installer.is_file():
    raise SystemExit("artifact is missing VERSION or install.sh")
if version_file.read_text(encoding="ascii") != version + "\n":
    raise SystemExit("artifact VERSION does not match the pinned version")
print(root)
PY
)

/bin/sh "$source_root/install.sh" "$@"
