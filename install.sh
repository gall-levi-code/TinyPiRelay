#!/bin/sh
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
applying=no
case "${1:-plan}" in
install|upgrade)
    for argument do
        [ "$argument" != --apply ] || applying=yes
    done
    ;;
esac
# Refresh package metadata only for applying deployment commands, never a
# read-only plan or uninstall. Do not execute os-release as shell code.
if [ "$applying" = yes ] && [ "$(id -u)" = 0 ] &&
   grep -Eq '^VERSION_CODENAME="?(bookworm|trixie)"?$' /etc/os-release &&
   { grep -Eq '^ID="?raspbian"?$' /etc/os-release ||
     grep -qi 'Raspberry Pi' /etc/rpi-issue 2>/dev/null; }; then
    echo "Refreshing package metadata (no distribution upgrade)"
    apt-get -o APT::Update::Error-Mode=any -o Acquire::Retries=2 \
        -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update
    if [ ! -x /usr/bin/python3 ]; then
        echo "Installing the Python 3 installer prerequisite"
        DEBIAN_FRONTEND=noninteractive apt-get --yes --no-remove --no-upgrade \
            --no-install-recommends install python3
    fi
fi
if [ ! -x /usr/bin/python3 ]; then
    echo "Python 3 is missing. On Raspberry Pi OS, run 'sudo /bin/sh ./install.sh install --apply' to install it automatically." >&2
    exit 2
fi
PYTHONPATH="$SCRIPT_DIR/src" exec /usr/bin/python3 -P -B -m tinypirelay.installer "$@"
