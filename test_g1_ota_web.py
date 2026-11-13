#!/usr/bin/env python3
"""Drive g1_ota_web.py over HTTP against test_g1_ota.py's fake arms.  No radio.

    test_g1_ota_web.py
"""
import json
import os
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_g1_ota as fake  # noqa: E402  (installs the fake BLE stack into g1_ota)
import g1_ota_web  # noqa: E402

import threading  # noqa: E402


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.loads(r.read())

    def post(self, path, fields=None, files=None):
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in (fields or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        for k, (name, data) in (files or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{name}\"\r\n"
                     f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait(self, what, timeout=120.0):
        """Poll /api/state until the scan or the job is no longer running."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            s = self.get("/api/state")
            cur = s["scan"] if what == "scan" else s["job"]
            if cur and cur["status"] != "running":
                return s
            time.sleep(0.1)
        raise AssertionError(f"{what} still running after {timeout}s")

    def log(self):
        return "\n".join(self.get("/api/state")["job"]["lines"])


def main():
    server = g1_ota_web.make_server(("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    c = Client(server.server_address[1])

    app = fake.make_image(0x00053EED, 300_000, b"app")
    old_app = fake.make_image(0x00053EED, 250_000, b"old")

    print("### 1. page and empty state")
    with urllib.request.urlopen(c.base + "/") as r:
        assert r.status == 200 and b"<title>G1 OTA</title>" in r.read()
    s = c.get("/api/state")
    assert s["arms"] == [] and s["job"] is None and s["scan"]["status"] == "idle"

    print("### 2. scan finds the arms, left first")
    fake.ARMS.clear()
    fake.ARMS["AA:R"] = fake.Arm("Even G1_44_R_BBBBBB", "AA:R", old_app)
    fake.ARMS["AA:L"] = fake.Arm("Even G1_44_L_AAAAAA", "AA:L", old_app, drop_every=23)
    fake.ARMS["zz"] = fake.Arm("Some Speaker", "zz", old_app)
    code, s = c.post("/api/scan")
    assert code == 200 and s["scan"]["status"] in ("running", "done"), s["scan"]
    s = c.wait("scan")
    assert [a["name"][11] for a in s["arms"]] == ["L", "R"], s["arms"]

    print("### 3. install on the left arm only, through the upload")
    code, s = c.post("/api/install", {"arms": "AA:L"}, {"file": ("app_update.bin", app)})
    assert code == 200 and s["job"]["status"] == "running" and s["busy"]
    code, err = c.post("/api/install", {"arms": "AA:L"}, {"file": ("app_update.bin", app)})
    assert code == 400 and "already running" in err["error"], err
    s = c.wait("job")
    assert s["job"]["status"] == "done", c.log()
    assert fake.ARMS["AA:L"].slots == {(0, 0): app} and fake.ARMS["AA:R"].slots == {(0, 0): old_app}
    assert fake.ARMS["AA:L"].drops > 0 and "installed" in c.log()
    # progress lines were committed into the log, none left dangling
    assert "%)" in c.log() and s["job"]["progress"] == ""

    print("### 4. bad upload -> error status with the script's message")
    code, s = c.post("/api/install", {"arms": "AA:R"}, {"file": ("junk.bin", b"hello" * 100)})
    assert code == 200
    s = c.wait("job")
    assert s["job"]["status"] == "error" and "neither an MCUboot" in s["job"]["error"], s["job"]

    print("### 5. no arm ticked / empty file -> 400 before anything starts")
    code, err = c.post("/api/install", {"arms": ""}, {"file": ("app_update.bin", app)})
    assert code == 400 and "tick" in err["error"], err
    code, err = c.post("/api/install", {"arms": "AA:R"}, {"file": ("app_update.bin", b"")})
    assert code == 400 and "empty" in err["error"], err

    print("### 6. cancel a slow install")
    fake.ARMS["AA:R"].latency, fake.ARMS["AA:R"].down_until = 0.3, 0.0
    code, s = c.post("/api/install", {"arms": "AA:R"}, {"file": ("app_update.bin", app)})
    assert code == 200, s
    time.sleep(1.5)
    code, s = c.post("/api/cancel")
    assert code == 200
    s = c.wait("job", timeout=10.0)
    assert s["job"]["status"] == "cancelled", s["job"]
    assert fake.ARMS["AA:R"].slots.get((0, 0)) == old_app and fake.ARMS["AA:R"].client is None

    print("### 7. adapters: listed, selectable, unknown one refused, choice reaches bleak")
    g1_ota_web.g1_ota.list_adapters = lambda: [("hci0", "AA:00"), ("hci1", "AA:11")]
    s = c.get("/api/state")
    assert [a["name"] for a in s["adapters"]] == ["hci0", "hci1"] and s["adapter"] is None
    code, err = c.post("/api/scan", {"adapter": "hci7"})
    assert code == 400 and "hci7" in err["error"], err
    fake.ADAPTERS_SEEN.clear()
    code, s = c.post("/api/scan", {"adapter": "hci1"})
    assert code == 200 and s["adapter"] == "hci1"
    c.wait("scan")
    fake.ARMS["AA:L"].down_until = fake.ARMS["AA:L"].latency = 0
    code, s = c.post("/api/install", {"arms": "AA:L", "adapter": "hci1"}, {"file": ("app_update.bin", app)})
    assert code == 200, s
    c.wait("job")
    assert fake.ADAPTERS_SEEN and set(fake.ADAPTERS_SEEN) == {"hci1"}, set(fake.ADAPTERS_SEEN)
    code, s = c.post("/api/scan", {"adapter": ""})
    assert code == 200 and s["adapter"] is None
    c.wait("scan")
    assert fake.ADAPTERS_SEEN[-1] is None

    print("### 8. incremental log fetch")
    full = c.get("/api/state")["job"]
    part = c.get(f"/api/state?since={full['total'] - 2}")["job"]
    assert part["lines"] == full["lines"][-2:] and part["total"] == full["total"]
    server.shutdown()
    print("ALL OK")


if __name__ == "__main__":
    main()
