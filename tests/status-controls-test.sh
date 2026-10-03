#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
mkdir "$tmp/bin" "$tmp/runtime" "$tmp/state"
chmod 700 "$tmp/runtime"

# The device list comes from $XDG_RUNTIME_DIR/devices so cases can swap it.
# 11:..: a Fast Pair headset (Google UUID fe2c, no Maestro service).
cat >"$tmp/bin/bluetoothctl" <<'EOF'
#!/bin/sh
case "$*" in
  "devices Connected") cat "$XDG_RUNTIME_DIR/devices" ;;
  "info AA:BB:CC:DD:EE:FF")
    printf '%s\n' "	Connected: yes" \
      "	UUID: Google                    (0000fe2c-0000-1000-8000-00805f9b34fb)" \
      "	UUID: Vendor specific           (25e97ff7-24ce-4c4c-8951-f764a708f7b5)" ;;
  "info 11:22:33:44:55:66")
    printf '%s\n' "	Connected: yes" \
      "	UUID: Google                    (0000fe2c-0000-1000-8000-00805f9b34fb)" ;;
  *) exit 1 ;;
esac
EOF

cat >"$tmp/bin/pbpctrl" <<'EOF'
#!/bin/sh
if [ "$*" = "set anc --help" ]; then
  if [ -f "${XDG_RUNTIME_DIR:-}/adaptive" ]; then
    echo "possible values: off, active, aware, adaptive"
  else
    echo "possible values: off, active, aware"
  fi
  exit 0
fi

[ "$1" = "-d" ] && shift 2
case "$*" in
  "show runtime")
    cat <<'OUT'
battery:
  case: 75% (not charging)
  left bud: 90% (not charging)
  right bud: 85% (charging)
placement:
  left bud: in ear
  right bud: in case
connection:
  state: connected
OUT
    ;;
  "get anc") echo active ;;
  "get multipoint") echo true ;;
  "get ohd") echo true ;;
  "get speech-detection") echo false ;;
  "get volume-exposure-notifications") echo true ;;
  "get volume-eq") echo false ;;
  "get mono") echo false ;;
  "get gestures") echo true ;;
  "get gesture-control") echo "left: anc, right: assistant" ;;
  "get anc-gesture-loop")
    if [ -f "${XDG_RUNTIME_DIR:-}/adaptive" ]; then
      echo "[active, aware, adaptive]"
    else
      echo "[active, aware]"
    fi
    ;;
  "get balance") echo "left: 80%, right: 100%" ;;
  "get eq") echo "[0.00, 1.50, -2.00, 0.50, 3.00]" ;;
  *) exit 1 ;;
esac
EOF

chmod +x "$tmp/bin/bluetoothctl" "$tmp/bin/pbpctrl"
# Stubs are selected by the trusted-dir allowlist, never ambient PATH.
export PIXELBUDS_TRUSTED_PATH="$tmp/bin"
export PATH="/usr/bin:/bin"
export XDG_RUNTIME_DIR="$tmp/runtime"
export XDG_STATE_HOME="$tmp/state"

assert_line() {
  printf '%s\n' "$1" | grep -Fx -- "$2" >/dev/null || {
    echo "missing output: $2" >&2
    exit 1
  }
}

echo "Device AA:BB:CC:DD:EE:FF Pixel Buds Pro" >"$tmp/runtime/devices"
legacy=$("$repo/status.sh" --controls)
assert_line "$legacy" "connected=1"
assert_line "$legacy" "adaptive_supported=0"
assert_line "$legacy" "anc=active"
assert_line "$legacy" "ctl_gestures=true"
assert_line "$legacy" "ctl_gesture_left=anc"
assert_line "$legacy" "ctl_gesture_right=assistant"
assert_line "$legacy" "ctl_anc_gesture_loop=active,aware"
assert_line "$legacy" "ctl_balance=20"
assert_line "$legacy" "ctl_eq=0.00,1.50,-2.00,0.50,3.00"

: >"$tmp/runtime/adaptive"
adaptive=$("$repo/status.sh" --controls)
assert_line "$adaptive" "adaptive_supported=1"
assert_line "$adaptive" "ctl_anc_gesture_loop=active,aware,adaptive"

# Renamed buds are still found by their Maestro service, behind another device.
printf '%s\n' "Device 11:22:33:44:55:66 Some Fast Pair Headset" \
  "Device AA:BB:CC:DD:EE:FF Ryan's Earbuds" >"$tmp/runtime/devices"
renamed=$("$repo/status.sh")
assert_line "$renamed" "connected=1"
assert_line "$renamed" "addr=AA:BB:CC:DD:EE:FF"
assert_line "$renamed" "name=Ryan's Earbuds"

# A Fast Pair headset is not Pixel Buds, even when it is named like one.
echo "Device 11:22:33:44:55:66 Pixel Buds Pro" >"$tmp/runtime/devices"
impostor=$("$repo/status.sh")
[ "$impostor" = "connected=0" ] || {
  echo "Fast Pair headset was detected as Pixel Buds: $impostor" >&2
  exit 1
}

echo "status controls test passed"
