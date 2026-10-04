#!/usr/bin/python3 -I
"""Real-hardware smoke test for the Pixel Buds bridge. NOT part of the
offline suite; run it only with the owner's consent while the buds are
connected.

    /usr/bin/python3 -I -B tests/hardware-smoke.py --yes-real-hardware
    /usr/bin/python3 -I -B tests/hardware-smoke.py --yes-real-hardware --write

Read-only by default: starts the bridge exactly as Service.qml does, waits for
the session, prints battery/placement/ANC, reads every control, times a
refresh, then closes stdin and checks the bridge exits and releases the link.

--write additionally performs one round trip on the listening mode: switch to
a different mode, confirm the buds report it, then restore the original mode
and confirm that too. Nothing else is ever written.

While it runs it holds the v1.x plugin's pbpctrl lock (if that plugin is
installed) so the old widget's pbpctrl calls cannot open a second RFCOMM
session at the same time; those calls simply time out for the duration.
"""
import fcntl
import json
import os
import select
import subprocess
import sys
import time

sys.dont_write_bytecode = True
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(REPO, "bridge", "pixelbuds_bridge.py")


class Bridge:
    def __init__(self):
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        for k in ("HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "DBUS_SYSTEM_BUS_ADDRESS"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        self.proc = subprocess.Popen(["/usr/bin/python3", "-I", "-B", BRIDGE], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.buf = b""
        self.next_id = 1

    def events(self, until, timeout):
        got = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                ev = json.loads(line)
                got.append(ev)
                if until(ev):
                    return got
            ready, _, _ = select.select([self.proc.stdout], [], [], 0.2)
            if ready:
                chunk = os.read(self.proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                self.buf += chunk
        raise SystemExit("FAIL: timed out / bridge ended; events so far: %r\nstderr: %r"
                         % (got, self.proc.stderr.read() if self.proc.poll() is not None else b""))

    def command(self, timeout=15, **cmd):
        cmd["id"] = self.next_id
        self.next_id += 1
        t0 = time.monotonic()
        self.proc.stdin.write((json.dumps(cmd) + "\n").encode())
        self.proc.stdin.flush()
        evs = self.events(lambda e: e.get("type") == "result" and e.get("id") == cmd["id"], timeout)
        return evs, time.monotonic() - t0


def last(evs, kind):
    found = [e for e in evs if e.get("type") == kind]
    return found[-1] if found else None


def hold_legacy_lock():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.geteuid()
    try:
        dfd = os.open(os.path.join(runtime, "omarchy-pixelbuds"), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        fd = os.open("pbpctrl.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    except OSError:
        return None
    finally:
        os.close(dfd)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def main(argv):
    if "--yes-real-hardware" not in argv:
        print(__doc__)
        return 2
    write = "--write" in argv
    legacy = hold_legacy_lock()
    print("legacy pbpctrl lock held:", legacy is not None)
    b = Bridge()
    t0 = time.monotonic()
    evs = b.events(lambda e: e.get("type") in ("ready", "bye"), 45)
    if evs[-1]["type"] == "bye":
        print("FAIL: bridge ended:", evs[-1])
        return 1
    print("session ready in %.2fs" % (time.monotonic() - t0))
    status = last(evs, "state")["status"]
    print("status:", json.dumps(status, indent=1))

    evs, dt = b.command(cmd="controls")
    print("controls (%.2fs):" % dt, json.dumps(last(evs, "controls")["controls"], indent=1))
    evs, dt = b.command(cmd="refresh")
    print("refresh: ok=%s in %.2fs" % (last(evs, "result")["ok"], dt))
    status = (last(evs, "state") or {"status": status})["status"]

    if write:
        original = status.get("anc")
        modes = ["off", "active", "aware"] + (["adaptive"] if status.get("adaptive_supported") == "1" else [])
        if original not in modes:
            print("FAIL: unknown current ANC mode %r; not writing" % original)
            return 1
        target = "aware" if original != "aware" else "active"
        for mode in (target, original):
            evs, dt = b.command(cmd="set_anc", mode=mode)
            res = last(evs, "result")
            now = last(evs, "state")["status"]["anc"] if last(evs, "state") else None
            print("set_anc %s: ok=%s reported=%s in %.2fs" % (mode, res["ok"], now, dt))
            if not res["ok"] or now != mode:
                print("FAIL: ANC round trip did not confirm %s" % mode)
                return 1
        print("ANC round trip OK (restored %s)" % original)

    b.proc.stdin.close()
    t0 = time.monotonic()
    code = b.proc.wait(5)
    print("bridge exit code %d after stdin EOF in %.2fs" % (code, time.monotonic() - t0))
    if legacy is not None:
        os.close(legacy)
    print("PASS" if code == 0 else "FAIL")
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
