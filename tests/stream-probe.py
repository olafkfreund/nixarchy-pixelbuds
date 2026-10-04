#!/usr/bin/python3 -I
"""Passive probe of the buds' Fast Pair message stream and GSND CONTROL
channel. NOT part of the offline suite; run it only with the owner's consent
while the buds are connected.

    /usr/bin/python3 -I -B tests/stream-probe.py --yes-real-hardware [--seconds 300] [--streams gsnd,gfps]

Streams (both by default):
  gfps  Fast Pair message stream, RFCOMM service df21fe2c-2515-4fdb-8886-f12c4d67927c
  gsnd  "GSND CONTROL", RFCOMM service f8d1fbe4-7966-4334-8024-ff96c9330e15

Each is opened exactly as the Maestro bridge opens Maestro: its own
client-role BlueZ Profile1 plus Device1.ConnectProfile, only if BlueZ reports
the buds Connected and ServicesResolved. It never calls Device1.Connect and
never reconnects.

Read-only: it sends NOTHING on either stream (pbpctrl's
libgfps/examples/gfps_listen.rs sends nothing either). Both use the same
framing: group u8, code u8, length u16 big-endian, payload; several messages
may share one read. Every message is printed with a monotonic timestamp, the
stream, group/code (named where known) and payload hex. GSND group 0x04 code
0x05 is decoded as the wear state reported in the opencontrolpixelbudspro2
captures (03 none worn, 04/05 one worn, 06 both worn, 01 in-ear detection
off). Ends after --seconds, on Ctrl-C, or when the buds disconnect; the
sockets are released on exit.

It also logs, read-only, every D-Bus signal BlueZ emits under the buds'
object path (property changes of Device1, MediaControl1, MediaPlayer1,
MediaTransport1 and so on), so a press-and-hold shows up if BlueZ sees
anything. HFP AT commands (e.g. AT+BVRA) are handled inside PipeWire and are
not visible here.

Suggested run: music playing, in-ear detection on. Wait 10 s, take one bud
out, wait 10 s, put it back, wait 10 s; repeat with the other bud; take both
out; dock one in the case. Then, with the right bud's hold action set to
the assistant, press and hold the right bud for ~3 s and release; repeat.
"""
import importlib.util
import os
import select
import signal
import sys
import threading
import time

sys.dont_write_bytecode = True
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GFPS_UUID = "df21fe2c-2515-4fdb-8886-f12c4d67927c"
GSND_UUID = "f8d1fbe4-7966-4334-8024-ff96c9330e15"
STREAMS = {"gfps": GFPS_UUID, "gsnd": GSND_UUID}
MAX_PAYLOAD = 4096          # libgfps MAX_FRAME_SIZE

GSND_CODES = {(0x04, 0x05): "WearState", (0x04, 0x14): "RightHoldAction", (0x04, 0x16): "HeadGesturesActive"}
WEAR = {0x01: "in-ear detection off", 0x03: "none worn", 0x04: "one worn", 0x05: "one worn (5)", 0x06: "both worn"}

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


def describe(stream, group, code, data):
    if stream == "gsnd":
        name = GSND_CODES.get((group, code), "?")
        extra = ""
        if (group, code) == (0x04, 0x05) and len(data) == 2 and data[0] == 0x08:
            extra = " wear=%s" % WEAR.get(data[1], "unknown value %d" % data[1])
        elif (group, code) == (0x04, 0x16) and len(data) == 2 and data[0] == 0x08:
            extra = " head_gestures=%s" % {1: "active", 2: "inactive"}.get(data[1], data[1])
        return "gsnd group=0x%02X code=0x%02X %s len=%d hex=%s%s" % (group, code, name, len(data), data.hex(), extra)
    extra = ""
    if group == 0x03 and code == 0x03 and data:
        extra = " batteries=[%s]" % ", ".join(battery(b) for b in data[:3])
    elif group == 0x03 and code == 0x06 and data:
        extra = " active_components=0b%s" % format(data[0], "08b")
    return "gfps %s(0x%02X) %s(0x%02X) len=%d hex=%s%s" % (
        GROUPS.get(group, "Unknown"), group, CODES.get(group, {}).get(code, "Unknown"), code,
        len(data), data.hex(), extra)


def watch_bluez(bluez, device, say):
    """Log every org.bluez signal under the device path. Read-only."""
    def on_signal(_conn, _sender, path, iface, signal, params):
        if path != device.path and not path.startswith(device.path + "/"):
            return
        try:
            args = repr(params.unpack())
        except Exception:
            args = "?"
        say("bluez %s %s.%s %s" % (path[len(device.path):] or "/", iface, signal, args[:400]))

    bluez._invoke(lambda: bluez.bus.signal_subscribe(
        "org.bluez", None, None, None, None, bluez.Gio.DBusSignalFlags.NONE, on_signal))


class Stream:
    def __init__(self, bridge, name, keep_r, terminate):
        self.name = name
        self.wake_r, self.wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.link = bridge.Link(self.wake_w)
        self.hub = bridge.Hub(bridge.LineReader(keep_r), self.wake_r, self.link, terminate)
        self.bluez = bridge.BluezGio(uuid=STREAMS[name], tag=name + "probe")
        self.sock = None
        self.buf = b""

    def frames(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 4:
            length = int.from_bytes(self.buf[2:4], "big")
            if length > MAX_PAYLOAD:
                out.append(("invalid", "invalid frame length %d; dropping hex=%s" % (length, self.buf[:16].hex())))
                self.buf = b""
                break
            if len(self.buf) < 4 + length:
                break
            out.append(("msg", (self.buf[0], self.buf[1], self.buf[4:4 + length])))
            self.buf = self.buf[4 + length:]
        return out

    def close(self):
        if self.sock is not None:
            try:
                self.sock.shutdown(2)
            except OSError:
                pass
            self.sock.close()
            self.sock = None
        self.bluez.close()


def main(argv):
    if "--yes-real-hardware" not in argv:
        print(__doc__)
        return 2
    seconds = 300.0
    if "--seconds" in argv:
        seconds = max(1.0, min(3600.0, float(argv[argv.index("--seconds") + 1])))
    names = ["gsnd", "gfps"]
    if "--streams" in argv:
        names = [n for n in argv[argv.index("--streams") + 1].split(",") if n in STREAMS]
    bridge = load_bridge()
    t0 = time.monotonic()

    def say(text):
        print("%9.3f %s" % (time.monotonic() - t0, text), flush=True)

    keep_r, keep_w = os.pipe()          # a stdin stand-in that never reaches EOF
    terminate = threading.Event()
    streams = [Stream(bridge, n, keep_r, terminate) for n in names]
    stop_all = lambda *_: [st.link.set_gone("terminated") for st in streams]
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop_all)

    first = streams[0].bluez
    device = first.find_device()
    if device is None:
        say("no connected Pixel Buds found")
        return 1
    props = first._call(device.path, "org.freedesktop.DBus.Properties", "GetAll",
                        first.GLib.Variant("(s)", ("org.bluez.Device1",)), "(a{sv})").unpack()[0]
    uuids = [str(u).lower() for u in props.get("UUIDs", [])]
    say("device %s (%s), connected=%s resolved=%s" % (device.addr, device.name, device.connected, device.resolved))
    if not device.resolved:
        say("services not resolved yet; try again in a moment")
        return 1
    try:
        for st in streams:
            if STREAMS[st.name] not in uuids:
                say("%s: not advertised by the buds; skipped" % st.name)
                continue
            try:
                st.bluez.start(device, st.link)
                st.sock = bridge.connect(st.bluez, device, st.link, st.hub)
                say("%s: stream open (sending nothing)" % st.name)
            except bridge.Stop as stop:
                say("%s: not opened: %s %s" % (st.name, stop.reason, stop.detail))
                if stop.reason in ("terminated", "disconnected"):
                    return 0
        watcher = next((st.bluez for st in streams if st.bluez.thread is not None), None)
        if watcher is not None:
            watch_bluez(watcher, device, say)
            say("bluez: logging signals under %s" % device.path)
        live = [st for st in streams if st.sock is not None]
        say("listening for %.0f s on %s" % (seconds, ", ".join(st.name for st in live) or "nothing"))
        end = time.monotonic() + seconds
        while live and time.monotonic() < end:
            rlist = [st.sock for st in live] + [st.wake_r for st in live]
            ready, _, _ = select.select(rlist, [], [], max(0.0, end - time.monotonic()))
            for st in list(live):
                if st.wake_r in ready:
                    try:
                        os.read(st.wake_r, 512)
                    except OSError:
                        pass
                if st.link.gone is not None:
                    say("%s: stopped: %s" % (st.name, st.link.gone))
                    return 0
                if st.sock in ready:
                    try:
                        data = st.sock.recv(4096)
                    except BlockingIOError:
                        continue
                    except OSError as error:
                        data = b""
                        say("%s: read error %s" % (st.name, error))
                    if not data:
                        say("%s: stream closed by the buds" % st.name)
                        st.close()
                        live.remove(st)
                        continue
                    for kind, item in st.frames(data):
                        if kind == "msg":
                            say(describe(st.name, *item))
                        else:
                            say("%s: %s" % (st.name, item))
        say("time is up" if live else "no stream left open")
    finally:
        for st in streams:
            st.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
