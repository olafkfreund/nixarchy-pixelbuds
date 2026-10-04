#!/bin/sh
# Runs every offline test. Needs no Bluetooth hardware and never touches the
# system bus or the desktop (the D-Bus test uses its own private dbus-daemon).
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
cd "$repo/tests/python"
env -u DBUS_SESSION_BUS_ADDRESS -u DBUS_SYSTEM_BUS_ADDRESS -u WAYLAND_DISPLAY -u DISPLAY \
  -u HYPRLAND_INSTANCE_SIGNATURE /usr/bin/python3 -B -m unittest discover -s . -p 'test_*.py' "$@"
sh "$repo/tests/launch-boundary-test.sh"
[ ! -e "$repo/bridge/__pycache__" ] && [ ! -e "$repo/tests/python/__pycache__" ] || {
  echo "bytecode was written" >&2
  exit 1
}
