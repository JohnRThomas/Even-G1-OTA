#!/usr/bin/env python3
"""Dry-run g1_ota.py against an in-process fake G1 arm.  No radio is involved.

    test_g1_ota.py [update.zip]

Without an argument the images are synthetic; with one, the app and net core
images of that update zip are used.  Takes about a minute.

The fake models Zephyr img_mgmt (upload with sha-resume and offset negotiation,
state read/set), os_mgmt reset, an overwrite-only MCUboot install on reboot,
and link drops.  It is a model of the firmware, not the firmware.
"""
import asyncio
import hashlib
import os
import struct
import sys
import tempfile
import time
import zipfile

import cbor2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import g1_ota  # noqa: E402

FRAG = 495


class Arm:
    def __init__(self, name, address, running_app, smp_reset=True, drop_every=0, latency=0.0):
        self.name, self.address = name, address
        self.slots = {(0, 0): running_app}          # (image, slot) -> bytes
        self.pending = set()
        self.up = None                              # in-progress upload state
        self.smp_reset, self.drop_every, self.latency = smp_reset, drop_every, latency
        self.down_until = 0.0
        self.client = None
        self.requests = self.heartbeats = self.drops = self.resets = self.resumes = 0
        self.max_nus_gap = 0.0
        self.last_nus = None

    def advertising(self):
        return time.monotonic() >= self.down_until and self.client is None

    def reboot(self):
        self.resets += 1
        self.up = None
        for image in sorted(self.pending):
            data = self.slots.pop((image, 1))
            if image == 0:
                self.slots[(0, 0)] = data           # net core primary is not readable
        self.pending.clear()
        self.down_until = time.monotonic() + 3.0
        self.disconnect()

    def disconnect(self):
        c, self.client = self.client, None
        if c is not None:
            c.is_connected = False
            if c.on_disconnect:
                c.on_disconnect(c)

    def smp(self, frame):
        op, _, length, group, seq, cmd = struct.unpack(">BBHHBB", frame[:8])
        req = cbor2.loads(frame[8:8 + length])
        rsp = self.handle(op, group, cmd, req)
        body = cbor2.dumps(rsp)
        return struct.pack(">BBHHBB", op + 1, 0, len(body), group, seq, cmd) + body

    def handle(self, op, group, cmd, req):
        if group == 1 and cmd == 0 and op == 0:
            images = []
            for (image, slot), data in sorted(self.slots.items()):
                try:
                    h = g1_ota.Image("x", data).hash
                except g1_ota.OtaError:
                    continue                         # partial upload: no valid TLVs
                images.append({"image": image, "slot": slot, "version": "0.0.0", "hash": h,
                               "bootable": True, "pending": image in self.pending and slot == 1,
                               "confirmed": slot == 0, "active": slot == 0, "permanent": False})
            return {"images": images, "splitStatus": 0}
        if group == 1 and cmd == 0 and op == 2:
            for (image, slot), data in self.slots.items():
                try:
                    if slot == 1 and g1_ota.Image("x", data).hash == req["hash"]:
                        self.pending.add(image)
                        return self.handle(0, 1, 0, {})
                except g1_ota.OtaError:
                    pass
            return {"rc": 5}
        if group == 1 and cmd == 1 and op == 2:
            if req["off"] == 0:
                image = req.get("image", 0)
                if image in self.pending:
                    return {"rc": 10}
                if self.up and self.up["sha"] == req["sha"] and self.up["len"] == req["len"]:
                    self.resumes += 1
                    return {"rc": 0, "off": self.up["off"]}      # resume, nothing written
                self.up = {"image": image, "len": req["len"], "sha": req["sha"], "off": 0}
                self.slots[(image, 1)] = b""
            elif self.up is None:
                return {"rc": 3}
            elif req["off"] != self.up["off"]:
                return {"rc": 0, "off": self.up["off"]}
            self.slots[(self.up["image"], 1)] += req["data"]
            self.up["off"] += len(req["data"])
            return {"rc": 0, "off": self.up["off"]}
        if group == 0 and cmd == 5 and op == 2:
            if not self.smp_reset:
                return {"rc": 8}
            asyncio.get_running_loop().call_later(0.25, self.reboot)
            return {}
        return {"rc": 8}


ARMS = {}


class Dev:
    def __init__(self, arm):
        self.name, self.address = arm.name, arm.address


class FakeScanner:
    @staticmethod
    async def discover(timeout=5.0):
        return [Dev(a) for a in ARMS.values() if a.advertising()] + [type("D", (), {"name": None, "address": "x"})()]

    @staticmethod
    async def find_device_by_address(address, timeout=10.0):
        end = time.monotonic() + min(timeout, 1.0)
        while time.monotonic() < end:
            if ARMS[address].advertising():
                return Dev(ARMS[address])
            await asyncio.sleep(0.1)
        return None


class Char:
    max_write_without_response_size = FRAG


class Services:
    def get_characteristic(self, uuid):
        return Char() if uuid == g1_ota.SMP_CHAR else None


class FakeClient:
    def __init__(self, device, disconnected_callback=None):
        self.arm = ARMS[device.address]
        self.on_disconnect = disconnected_callback
        self.is_connected = False
        self.services = Services()
        self.buf = bytearray()
        self.notify = None

    async def connect(self):
        if not self.arm.advertising():
            raise RuntimeError("fake: device not reachable")
        self.arm.client = self
        self.arm.last_nus = time.monotonic()
        self.is_connected = True

    async def disconnect(self):
        if self.arm.client is self:
            self.arm.client = None
        self.is_connected = False

    async def start_notify(self, char, cb):
        self.notify = cb

    async def write_gatt_char(self, char, data, response=None):
        if not self.is_connected:
            raise RuntimeError("fake: not connected")
        arm = self.arm
        if char == g1_ota.NUS_RX_CHAR:
            arm.max_nus_gap = max(arm.max_nus_gap, time.monotonic() - arm.last_nus)
            arm.last_nus = time.monotonic()
            if bytes(data) == b"#r":
                asyncio.get_running_loop().call_later(0.1, arm.reboot)
            else:
                assert bytes(data) == bytes([0x25, 6, 0, 0, 4, 0]), data
                arm.heartbeats += 1
            return
        assert len(data) <= FRAG
        self.buf += data
        if len(self.buf) < 8 or len(self.buf) < 8 + struct.unpack_from(">H", self.buf, 2)[0]:
            return
        frame, self.buf = bytes(self.buf), bytearray()
        assert len(frame) <= 2475, len(frame)
        arm.requests += 1
        if arm.drop_every and arm.requests % arm.drop_every == 0:
            arm.drops += 1
            # alternate: drop before the request is handled / after (response lost)
            if arm.drops % 2 == 0:
                arm.smp(frame)
            asyncio.get_running_loop().call_later(0.05, arm.disconnect)
            return
        rsp = arm.smp(frame)
        asyncio.get_running_loop().create_task(self._deliver(rsp))

    async def _deliver(self, rsp):
        await asyncio.sleep(self.arm.latency)
        for i in range(0, len(rsp), 20):          # tiny notifications: exercise reassembly
            if self.notify and self.is_connected:
                self.notify(None, bytearray(rsp[i:i + 20]))


g1_ota.BleakScanner = FakeScanner
g1_ota.BleakClient = FakeClient


def run(path):
    try:
        asyncio.run(g1_ota.main(path))
        return None
    except g1_ota.OtaError as e:
        return str(e)


def make_image(reset, size, seed):
    """A structurally valid MCUboot image: header, body, SHA256 TLV (unsigned)."""
    body = struct.pack("<II", 0x20030000, reset)
    while len(body) < size:
        body += hashlib.sha256(seed + len(body).to_bytes(4, "little")).digest()
    hdr = struct.pack("<IIHHII", g1_ota.MCUBOOT_MAGIC, 0, 0x200, 0, len(body), 0).ljust(0x200, b"\xff")
    tlv = struct.pack("<HHHH", 0x6907, 4 + 4 + 32, g1_ota.TLV_SHA256, 32)
    return hdr + body + tlv + hashlib.sha256(hdr + body).digest()


def main(tmp):
    if len(sys.argv) > 1:
        with zipfile.ZipFile(sys.argv[1]) as z:
            app, net = z.read("app_update.bin"), z.read("net_core_app_update.bin")
    else:
        app = make_image(0x00053EED, 900_000, b"app")
        net = make_image(0x0102FC4D, 200_000, b"net")
    old_app = make_image(0x00053EED, 800_000, b"old")
    zpath = f"{tmp}/application_update.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("app_update.bin", app)
        z.writestr("net_core_app_update.bin", net)
        z.writestr("manifest.json", "{}")
    for name, data in (("app_update.bin", app), ("net_core_app_update.bin", net),
                       ("junk.bin", b"hello" * 100), ("trunc.bin", app[:-100])):
        with open(f"{tmp}/{name}", "wb") as fh:
            fh.write(data)
    with zipfile.ZipFile(f"{tmp}/dup.zip", "w") as z:
        z.writestr("a.bin", app)
        z.writestr("b.bin", app)

    print("### 1. zip -> two arms, link drops on the left, SMP reset refused on the right")
    ARMS.clear()
    ARMS["AA:L"] = Arm("Even G1_44_L_AAAAAA", "AA:L", old_app, drop_every=37)
    ARMS["AA:R"] = Arm("Even G1_44_R_BBBBBB", "AA:R", old_app, smp_reset=False, latency=0.03)
    ARMS["zz"] = Arm("Some Speaker", "zz", old_app)
    err = run(zpath)
    assert err is None, err
    for a in (ARMS["AA:L"], ARMS["AA:R"]):
        assert a.slots == {(0, 0): app}, (a.name, {k: len(v) for k, v in a.slots.items()})
        assert a.resets == 1 and not a.pending
    L, R = ARMS["AA:L"], ARMS["AA:R"]
    assert L.drops > 5 and L.resumes > 0, (L.drops, L.resumes)
    assert R.drops == 0 and R.heartbeats >= 1 and R.max_nus_gap < 15.0, (R.heartbeats, R.max_nus_gap)
    assert ARMS["zz"].requests == 0
    print(f"    left: drops={L.drops} resumes={L.resumes}; right: heartbeats={R.heartbeats} "
          f"max NUS gap={R.max_nus_gap:.1f}s")

    print("### 2. same zip again: app already running, net core is re-flashed")
    for a in ARMS.values():
        a.drop_every, a.latency, a.requests = 0, 0.0, 0
    assert run(zpath) is None
    assert ARMS["AA:L"].resets == 2 and ARMS["AA:L"].slots == {(0, 0): app}

    print("### 3. app_update.bin alone, already running -> nothing to do")
    assert run(f"{tmp}/app_update.bin") is None
    assert ARMS["AA:L"].resets == 2 and ARMS["AA:R"].resets == 2

    print("### 4. net_core_app_update.bin alone -> image 1 only")
    assert run(f"{tmp}/net_core_app_update.bin") is None
    assert ARMS["AA:L"].resets == 3 and ARMS["AA:L"].slots == {(0, 0): app}

    print("### 5. app .bin already staged by an interrupted run -> armed without re-upload")
    ARMS.clear()
    a = ARMS["AA:L"] = Arm("Even G1_44_L_AAAAAA", "AA:L", old_app)
    a.slots[(0, 1)] = app
    assert run(f"{tmp}/app_update.bin") is None
    assert a.slots == {(0, 0): app} and a.resets == 1 and a.up is None

    print("### 6. arm never reboots -> reported as not installed")
    ARMS.clear()
    a = ARMS["AA:L"] = Arm("Even G1_44_L_AAAAAA", "AA:L", old_app, smp_reset=False)
    a.reboot = lambda: None
    err = run(f"{tmp}/app_update.bin")
    assert err and "was not installed" in err, err
    print("    ->", err)

    print("### 7. bad inputs")
    for bad in ("junk.bin", "trunc.bin", "dup.zip"):
        err = run(f"{tmp}/{bad}")
        assert err, bad
        print("    ->", err)

    print("### 8. nothing in range")
    ARMS.clear()
    err = run(zpath)
    assert err and "no Even G1 found" in err, err
    print("ALL OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        main(tmp)
