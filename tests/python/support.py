"""Shared test support: module loading and a scripted fake Pixel Buds.

Nothing here touches Bluetooth hardware, the system bus or the desktop: the
fake buds speak the Maestro protocol over one end of a socketpair.
"""
import importlib.util
import os
import socket
import sys
import threading

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRIDGE_DIR = os.path.join(REPO, "bridge")


def load(name):
    spec = importlib.util.spec_from_file_location("t_" + name, os.path.join(BRIDGE_DIR, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load("pixelbuds_bridge")
maestro = bridge.maestro
m = maestro


def runtime_payload(case=(75, 1), left=(90, 1), right=(85, 2), left_in_case=False, right_in_case=True):
    def dev(v):
        return m.f_varint(1, v[0]) + m.f_varint(2, v[1])
    bat = b""
    if case is not None:
        bat += m.f_bytes(1, dev(case))
    if left is not None:
        bat += m.f_bytes(2, dev(left))
    if right is not None:
        bat += m.f_bytes(3, dev(right))
    place = b""
    if right_in_case:
        place += m.f_varint(1, 1)
    if left_in_case:
        place += m.f_varint(2, 1)
    return m.f_varint(2, 12345) + m.f_bytes(6, bat) + m.f_bytes(7, place)


def default_settings():
    return {
        m.S_ANC: m.sv_anc("active"),
        m.S_MULTIPOINT: m.sv_bool(m.S_MULTIPOINT, True),
        m.S_OHD: m.sv_bool(m.S_OHD, True),
        m.S_SPEECH_DETECTION: m.sv_bool(m.S_SPEECH_DETECTION, False),
        m.S_VOLUME_EXPOSURE: m.sv_bool(m.S_VOLUME_EXPOSURE, True),
        m.S_VOLUME_EQ: m.sv_bool(m.S_VOLUME_EQ, False),
        m.S_MONO: m.sv_bool(m.S_MONO, False),
        m.S_GESTURES: m.sv_bool(m.S_GESTURES, True),
        m.S_GESTURE_CONTROL: m.sv_gesture_control("anc", "assistant"),
        m.S_ANC_GESTURE_LOOP: m.sv_anc_gesture_loop(["active", "aware"]),
        m.S_BALANCE: m.sv_balance(20),
        m.S_EQ: m.sv_eq([0.0, 1.5, -2.0, 0.5, 3.0]),
    }


class FakeBuds(threading.Thread):
    """Minimal Maestro server over a socket."""

    def __init__(self, sock, channel=19, announce=True, settings=None, runtime=None):
        super().__init__(daemon=True)
        self.sock = sock
        self.channel = channel
        self.announce = announce
        self.settings = default_settings() if settings is None else settings
        self.runtime = runtime_payload() if runtime is None else runtime
        self.silent_methods = set()      # method paths that never get a reply
        self.requests = []               # (method path, payload)
        self.writes = []                 # SettingValue bytes written
        self.cancels = []
        self.subs = {}                   # method path -> uid
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.names = {m.path_ids(p): p for p in (
            m.M_GET_SOFTWARE_INFO, m.M_SUB_RUNTIME_INFO, m.M_WRITE_SETTING,
            m.M_READ_SETTING, m.M_SUB_SETTINGS)}

    def send_packet(self, packet, channel=None):
        ch = self.channel if channel is None else channel
        data = m.hdlc_encode(m.address_for_channel(ch), packet.encode())
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass

    def send_raw(self, data):
        with self.lock:
            self.sock.sendall(data)

    def reply(self, req, payload=b"", ptype=m.PT_RESPONSE, status=0):
        self.send_packet(m.RpcPacket(ptype, req.channel_id, req.service_id, req.method_id,
                                     payload, status, req.call_id))

    def push_runtime(self, payload):
        uid = self.subs.get(m.M_SUB_RUNTIME_INFO)
        if uid:
            self.send_packet(m.RpcPacket(m.PT_SERVER_STREAM, uid[0], uid[1], uid[2], payload, 0, uid[3]))

    def push_setting(self, sv):
        uid = self.subs.get(m.M_SUB_SETTINGS)
        if uid:
            self.send_packet(m.RpcPacket(m.PT_SERVER_STREAM, uid[0], uid[1], uid[2], m.f_bytes(4, sv), 0, uid[3]))

    def run(self):
        dec = m.HdlcDecoder()
        if self.announce:
            svc, meth = m.path_ids(m.M_GET_SOFTWARE_INFO)
            self.send_packet(m.RpcPacket(m.PT_RESPONSE, self.channel, svc, meth, b"", 0, m.OPEN_CALL_ID))
        while True:
            try:
                data = self.sock.recv(4096)
            except OSError:
                break
            if not data:
                break
            for frame in dec.feed(data):
                pkt = m.decode_rpc_frame(frame)
                if pkt is not None:
                    self.handle(pkt)
        self.stopped.set()

    def handle(self, pkt):
        path = self.names.get((pkt.service_id, pkt.method_id), "?")
        if pkt.type == m.PT_CLIENT_ERROR:
            self.cancels.append(path)
            if self.subs.get(path) == pkt.uid():
                del self.subs[path]
            return
        self.requests.append((path, pkt.payload))
        if path in self.silent_methods:
            return
        if pkt.channel_id != self.channel:
            return                                   # wrong channel: no answer
        if path == m.M_GET_SOFTWARE_INFO:
            self.reply(pkt)
        elif path == m.M_READ_SETTING:
            sid = m.get_uint(m.parse_fields(pkt.payload), 4)
            if sid in self.settings:
                self.reply(pkt, m.f_bytes(4, self.settings[sid]))
            else:
                self.reply(pkt, ptype=m.PT_SERVER_ERROR, status=2)
        elif path == m.M_WRITE_SETTING:
            sv = m.get_bytes(m.parse_fields(pkt.payload), 4)
            fields = m.parse_fields(sv)
            sid = next(iter(fields))
            self.settings[sid] = sv
            self.writes.append(sv)
            self.reply(pkt)
            self.push_setting(sv)
        elif path == m.M_SUB_RUNTIME_INFO:
            self.subs[path] = pkt.uid()
            self.push_runtime(self.runtime)
        elif path == m.M_SUB_SETTINGS:
            self.subs[path] = pkt.uid()


class Collector:
    """File-like stdout capture that parses JSON lines."""

    def __init__(self):
        import json
        self.json = json
        self.lines = []
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            self.lines.append(text)

    def flush(self):
        pass

    def events(self):
        out = []
        for chunk in "".join(self.lines).splitlines():
            out.append(self.json.loads(chunk))
        return out

    def of(self, kind):
        return [e for e in self.events() if e.get("type") == kind]


class FakeBluez:
    """Stands in for BluezGio: one connected device, ConnectProfile hands the
    bridge one end of a socketpair whose other end is a FakeBuds."""

    def __init__(self, device=None, resolved=True, connected=True, buds_kwargs=None, fail_connect=None):
        self.device = device if device is not None else bridge.Device(
            "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF", "AA:BB:CC:DD:EE:FF", "Pixel Buds Pro", 0x240404,
            True, resolved)
        self.connected = connected
        self.resolved = resolved
        self.buds_kwargs = buds_kwargs or {}
        self.fail_connect = fail_connect
        self.buds = None
        self.link = None
        self.connect_calls = 0
        self.closed = False

    def find_device(self):
        return self.device

    def device_state(self, path):
        return self.connected, self.resolved

    def start(self, device, link):
        self.link = link

    def connect_profile(self):
        self.connect_calls += 1
        if self.fail_connect:
            self.link.set_connect_error(self.fail_connect)
            return
        ours, theirs = socket.socketpair()
        self.buds = FakeBuds(theirs, **self.buds_kwargs)
        self.buds.start()
        self.link.offer_fd(ours.detach())

    def close(self):
        self.closed = True
