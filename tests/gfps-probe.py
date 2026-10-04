#!/usr/bin/python3 -I
"""Passive Google Fast Pair message-stream probe. NOT part of the offline
suite; run it only with the owner's consent while the buds are connected.

    /usr/bin/python3 -I -B tests/gfps-probe.py --yes-real-hardware [--seconds 300]

Connects to the buds' Fast Pair message stream (RFCOMM service
df21fe2c-2515-4fdb-8886-f12c4d67927c) exactly as the Maestro bridge connects
to Maestro: a client-role BlueZ Profile1 for that UUID plus
Device1.ConnectProfile, only if BlueZ reports the buds Connected and
ServicesResolved. It never calls Device1.Connect and never reconnects.

It is read-only: like pbpctrl's libgfps/examples/gfps_listen.rs (whose
message framing it follows: group u8, code u8, length u16 big-endian,
payload) it sends NOTHING on the stream. Every received message is printed
as one line with a monotonic timestamp, group/code (named where libgfps
names them) and the payload in hex. Ends after --seconds, on Ctrl-C, or when
the buds disconnect; the RFCOMM socket is released on exit.

Suggested run: start it, wait 10 s, then take one bud out, wait 10 s, put it
back, wait 10 s; repeat with the other bud, then dock one in the case.
"""
import importlib.util
import os
import signal
import sys
import threading
import time

sys.dont_write_bytecode = True
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GFPS_UUID = "df21fe2c-2515-4fdb-8886-f12c4d67927c"
MAX_PAYLOAD = 4096          # libgfps MAX_FRAME_SIZE

GROUPS = {0x01: "Bluetooth", 0x02: "Logging", 0x03: "Device", 0x04: "DeviceAction",
          0x05: "DeviceConfiguration", 0x06: "DeviceCapabilitySync",
          0x07: "SmartAudioSourceSwitching", 0xFF: "Acknowledgement"}
CODES = {
    0x01: {0x01: "EnableSilenceMode", 0x02: "DisableSilenceMode"},
    0x02: {0x01: "LogFull", 0x02: "LogSaveToBuffer"},
    0x03: {0x01: "ModelId", 0x02: "BleAddress", 0x03: "BatteryInfo", 0x04: "BatteryTime",
           0x05: "ActiveComponentsRequest", 0x06: "ActiveComponentsResponse", 0x07: "Capability",
           0x08: "PlatformType", 0x09: "FirmwareVersion", 0x0A: "SectionNonce"},
    0x04: {0x01: "Ring"},
    0x05: {0x01: "BufferSize"},
    0x06: {0x01: "CapabilityUpdate", 0x02: "ConfigurableBufferSizeRange"},
    0x07: {0x10: "GetCapabilityOfSass", 0x11: "NotifyCapabilityOfSass", 0x12: "SetMultiPointState",
           0x30: "SwitchAudioSourceBetweenConnectedDevices", 0x31: "SwitchBack",
           0x32: "NotifyMultiPointSwitchEvent", 0x33: "GetConnectionStatus",
           0x34: "NotifyConnectionStatus", 0x40: "SassInitiatedConnection",
           0x41: "IndicateInUseAccountKey", 0x42: "SetCustomData"},
    0xFF: {0x01: "Ack", 0x02: "Nak"},
}


def load_bridge():
    spec = importlib.util.spec_from_file_location("pixelbuds_bridge", os.path.join(REPO, "bridge", "pixelbuds_bridge.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def battery(byte):
    if byte & 0x7F == 0x7F:
        return "unknown"
    return "%d%%%s" % (byte & 0x7F, " charging" if byte & 0x80 else "")


def describe(group, code, data):
    extra = ""
    if group == 0x03 and code == 0x03 and data:
        extra = " batteries=[%s]" % ", ".join(battery(b) for b in data[:3])
    elif group == 0x03 and code == 0x06 and data:
        extra = " active_components=0b%s" % format(data[0], "08b")
    return "%s(0x%02X) %s(0x%02X) len=%d hex=%s%s" % (
        GROUPS.get(group, "Unknown"), group, CODES.get(group, {}).get(code, "Unknown"), code,
        len(data), data.hex(), extra)


def main(argv):
    if "--yes-real-hardware" not in argv:
        print(__doc__)
        return 2
    seconds = 300.0
    if "--seconds" in argv:
        seconds = max(1.0, min(3600.0, float(argv[argv.index("--seconds") + 1])))
    bridge = load_bridge()

    keep_r, keep_w = os.pipe()          # a stdin stand-in that never reaches EOF
    wake_r, wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    link = bridge.Link(wake_w)
    terminate = threading.Event()
    hub = bridge.Hub(bridge.LineReader(keep_r), wake_r, link, terminate)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: link.set_gone("terminated"))
    signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)

    bluez = bridge.BluezGio(uuid=GFPS_UUID, tag="gfpsprobe")
    device = bluez.find_device()
    if device is None:
        print("no connected Pixel Buds found")
        return 1
    props = bluez._call(device.path, "org.freedesktop.DBus.Properties", "GetAll",
                        bluez.GLib.Variant("(s)", ("org.bluez.Device1",)), "(a{sv})").unpack()[0]
    if GFPS_UUID not in [str(u).lower() for u in props.get("UUIDs", [])]:
        print("the buds do not advertise the Fast Pair message stream")
        return 1
    print("device %s (%s), connected=%s resolved=%s" % (device.addr, device.name, device.connected, device.resolved),
          flush=True)
    if not device.resolved:
        print("services not resolved yet; try again in a moment")
        return 1
    sock = None
    t0 = time.monotonic()
    try:
        bluez.start(device, link)
        sock = bridge.connect(bluez, device, link, hub)
        hub.sock = sock
        print("%9.3f stream open; listening for %.0f s (sending nothing)" % (time.monotonic() - t0, seconds),
              flush=True)
        buf = b""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            buf += hub.wait(end)
            while len(buf) >= 4:
                length = int.from_bytes(buf[2:4], "big")
                if length > MAX_PAYLOAD:
                    print("%9.3f invalid frame length %d; dropping buffer hex=%s"
                          % (time.monotonic() - t0, length, buf[:16].hex()), flush=True)
                    buf = b""
                    break
                if len(buf) < 4 + length:
                    break
                group, code, data = buf[0], buf[1], buf[4:4 + length]
                buf = buf[4 + length:]
                print("%9.3f %s" % (time.monotonic() - t0, describe(group, code, data)), flush=True)
            if len(buf) > 4 + MAX_PAYLOAD:
                buf = b""
        print("%9.3f time is up" % (time.monotonic() - t0))
    except bridge.Stop as stop:
        print("%9.3f stopped: %s %s" % (time.monotonic() - t0, stop.reason, stop.detail), flush=True)
    finally:
        if sock is not None:
            try:
                sock.shutdown(2)
            except OSError:
                pass
            sock.close()
        bluez.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
