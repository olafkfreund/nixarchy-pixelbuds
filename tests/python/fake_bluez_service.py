"""A fake org.bluez for the D-Bus integration test.

Runs on a PRIVATE dbus-daemon started by the test (never the system bus).
Implements just enough of BlueZ for pixelbuds_bridge.BluezGio: the object
manager, Device1 properties and ConnectProfile, and ProfileManager1. On
ConnectProfile it creates a socketpair, serves the Maestro protocol on one end
with FakeBuds and hands the other end to the registered Profile1 through
NewConnection, exactly as bluetoothd hands over an RFCOMM socket.

Test hooks on the device object (interface test.Control):
  Disconnect()            emit Connected=false like a user disconnect
  RequestDisconnection()  call the profile's RequestDisconnection
"""
import os
import socket
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi  # noqa: E402
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from support import FakeBuds, m  # noqa: E402

DEV = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
GFPS_UUID = "df21fe2c-2515-4fdb-8886-f12c4d67927c"
# Scripted Fast Pair messages: ModelId, then BatteryInfo (L 90%, R 85% charging, case unknown).
GFPS_SCRIPT = bytes([0x03, 0x01, 0x00, 0x03, 0x12, 0x34, 0x56,
                     0x03, 0x03, 0x00, 0x03, 90, 0x80 | 85, 0xFF])
GSND_UUID = "f8d1fbe4-7966-4334-8024-ff96c9330e15"
# Scripted GSND CONTROL: wear "both", then two messages batched in one write.
GSND_SCRIPT = [bytes([0x04, 0x05, 0x00, 0x02, 0x08, 0x06]),
               bytes([0x04, 0x05, 0x00, 0x02, 0x08, 0x04, 0x04, 0x16, 0x00, 0x02, 0x08, 0x01])]
SCRIPTS = {GFPS_UUID: [GFPS_SCRIPT], GSND_UUID: GSND_SCRIPT}
XML = """
<node>
  <interface name="org.freedesktop.DBus.ObjectManager">
    <method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
  </interface>
  <interface name="org.bluez.ProfileManager1">
    <method name="RegisterProfile"><arg type="o" direction="in"/><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/></method>
    <method name="UnregisterProfile"><arg type="o" direction="in"/></method>
  </interface>
  <interface name="org.bluez.Device1">
    <method name="ConnectProfile"><arg type="s" direction="in"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="GetAll"><arg type="s" direction="in"/><arg type="a{sv}" direction="out"/></method>
  </interface>
  <interface name="test.Control">
    <method name="Disconnect"/>
    <method name="EmitMediaProps"/>
    <method name="RequestDisconnection"/>
    <method name="Stats"><arg type="a{si}" direction="out"/></method>
  </interface>
</node>
"""

state = {"connected": True, "resolved": True, "profile": None, "profiles": {}, "connects": 0, "buds": None,
         "gfps_received": 0}


def props():
    return {
        "Address": GLib.Variant("s", "AA:BB:CC:DD:EE:FF"),
        "Alias": GLib.Variant("s", "Pixel Buds Pro 2"),
        "Class": GLib.Variant("u", 0x244404),
        "Connected": GLib.Variant("b", state["connected"]),
        "ServicesResolved": GLib.Variant("b", state["resolved"]),
        "UUIDs": GLib.Variant("as", ["0000fe2c-0000-1000-8000-00805f9b34fb", m.MAESTRO_UUID, GFPS_UUID, GSND_UUID]),
    }


def main():
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    node = Gio.DBusNodeInfo.new_for_xml(XML)
    ifaces = {i.name: i for i in node.interfaces}

    def handler(conn, sender, path, iface, method, params, inv):
        if method == "GetManagedObjects":
            inv.return_value(GLib.Variant("(a{oa{sa{sv}}})", ({DEV: {"org.bluez.Device1": props()}},)))
        elif method == "RegisterProfile":
            ppath, uuid, _opts = params.unpack()
            state["profiles"][uuid] = (sender, ppath, uuid)
            if uuid == m.MAESTRO_UUID:
                state["profile"] = (sender, ppath, uuid)
            inv.return_value(None)
        elif method == "UnregisterProfile":
            (ppath,) = params.unpack()
            for uuid, entry in list(state["profiles"].items()):
                if entry[1] == ppath:
                    del state["profiles"][uuid]
                    if uuid == m.MAESTRO_UUID:
                        state["profile"] = None
            inv.return_value(None)
        elif method == "GetAll":
            inv.return_value(GLib.Variant("(a{sv})", (props(),)))
        elif method == "ConnectProfile":
            (uuid,) = params.unpack()
            state["connects"] += 1
            profile = state["profiles"].get(uuid)
            if profile is None or not state["connected"]:
                inv.return_dbus_error("org.bluez.Error.Failed", "no profile")
                return
            ours, theirs = socket.socketpair()
            if uuid in SCRIPTS:
                for chunk in SCRIPTS[uuid]:
                    theirs.sendall(chunk)
                state.setdefault("socks", []).append(theirs)   # keep open; count anything sent to us

                def drain(sock=theirs):
                    while True:
                        try:
                            data = sock.recv(4096)
                        except OSError:
                            return
                        if not data:
                            return
                        state["gfps_received"] += len(data)
                import threading
                threading.Thread(target=drain, daemon=True).start()
            else:
                state["buds"] = FakeBuds(theirs)
                state["buds"].start()
            fds = Gio.UnixFDList.new()
            fds.append(ours.fileno())
            ours.close()
            owner, ppath, _ = profile

            def done(c, res):
                try:
                    c.call_with_unix_fd_list_finish(res)
                    inv.return_value(None)
                except Exception as error:
                    inv.return_dbus_error("org.bluez.Error.Failed", str(error))

            bus.call_with_unix_fd_list(owner, ppath, "org.bluez.Profile1", "NewConnection",
                                       GLib.Variant("(oha{sv})", (DEV, 0, {})), None,
                                       Gio.DBusCallFlags.NONE, 5000, fds, None, done)
        elif method == "Disconnect":
            state["connected"] = False
            state["resolved"] = False
            bus.emit_signal(None, DEV, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                            GLib.Variant("(sa{sv}as)", ("org.bluez.Device1",
                                                        {"Connected": GLib.Variant("b", False)}, [])))
            inv.return_value(None)
        elif method == "EmitMediaProps":
            bus.emit_signal(None, DEV + "/player0", "org.freedesktop.DBus.Properties", "PropertiesChanged",
                            GLib.Variant("(sa{sv}as)", ("org.bluez.MediaPlayer1",
                                                        {"Status": GLib.Variant("s", "paused")}, [])))
            inv.return_value(None)
        elif method == "RequestDisconnection":
            owner, ppath, _ = state["profile"]
            bus.call_sync(owner, ppath, "org.bluez.Profile1", "RequestDisconnection",
                          GLib.Variant("(o)", (DEV,)), None, Gio.DBusCallFlags.NONE, 3000, None)
            inv.return_value(None)
        elif method == "Stats":
            buds = state["buds"]
            inv.return_value(GLib.Variant("(a{si})", ({
                "connects": state["connects"],
                "registered": len(state["profiles"]),
                "buds_stopped": 1 if buds is not None and buds.stopped.is_set() else 0,
                "gfps_received": state["gfps_received"],
            },)))
        else:
            inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    register = getattr(bus, "register_object_with_closures2", None) or bus.register_object
    register("/", ifaces["org.freedesktop.DBus.ObjectManager"], handler, None, None)
    register("/org/bluez", ifaces["org.bluez.ProfileManager1"], handler, None, None)
    for name in ("org.bluez.Device1", "org.freedesktop.DBus.Properties", "test.Control"):
        register(DEV, ifaces[name], handler, None, None)
    loop = GLib.MainLoop()

    def acquired(*_):
        print("ready", flush=True)

    Gio.bus_own_name_on_connection(bus, "org.bluez", Gio.BusNameOwnerFlags.NONE, acquired, lambda *_: loop.quit())
    loop.run()


if __name__ == "__main__":
    main()
