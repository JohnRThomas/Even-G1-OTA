#!/usr/bin/env python3
"""Flash Even Realities G1 firmware over BLE.

    g1_ota.py <application_update.zip | app_update.bin | net_core_app_update.bin>
    g1_ota.py --adapter hci1 ...     use that Bluetooth adapter (Linux; default: the system's)

Every G1 arm in range is flashed in turn (left, then right).  The update is
stock mcumgr/SMP into the MCUboot secondary slot.  THERE IS NO REVERT: the G1
bootloader is overwrite-only, so the old image is gone as soon as the arm reboots.
"""
import argparse
import asyncio
import hashlib
import io
import os
import re
import struct
import sys
import time
import zipfile

try:
    import cbor2
    from bleak import BleakClient, BleakScanner
except ImportError as e:
    sys.exit(f"{e}.  The OTA tools need the packages in tools/ota/requirements.txt: run "
             f"tools/ota/setup.sh once, then use tools/ota/.venv/bin/python instead of python3.")

NAME_PREFIX = "Even G1_"
ADAPTER = None   # local Bluetooth adapter to use (BlueZ name such as "hci1"); None = default
SMP_CHAR = "da2e7828-fbce-4e01-ae9e-261174997c48"     # mcumgr SMP, write + notify
NUS_RX_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # G1 command channel, write

OP_READ, OP_WRITE = 0, 2
GROUP_OS, GROUP_IMAGE = 0, 1
OS_RESET = 5
IMG_STATE, IMG_UPLOAD = 0, 1

# The firmware reassembles one SMP request of up to 2475 bytes from several
# GATT writes; leave room for the SMP header and the CBOR keys.
CHUNK = 2475 - 160

MCUBOOT_MAGIC = 0x96F3B83D
TLV_MAGICS = (0x6907, 0x6908)
TLV_SHA256 = 0x10
CORE = {0: "app core", 1: "net core"}
SLOT_SIZE = {0: 0xF0000, 1: 0x40000}


class OtaError(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def adapter_kw():
    """Keyword arguments that steer bleak to ADAPTER (ignored off Linux)."""
    return {"adapter": ADAPTER} if ADAPTER else {}


def list_adapters():
    """[(name, label)] of the local Bluetooth adapters (Linux: /sys/class/bluetooth).
    The label is the adapter's address where sysfs has it, else the USB
    device's manufacturer and product, so two dongles can be told apart."""
    base = "/sys/class/bluetooth"
    try:
        names = sorted(n for n in os.listdir(base) if re.fullmatch(r"hci\d+", n))
    except OSError:
        return []

    def read(path):
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError:
            return ""

    out = []
    for n in names:
        label = read(f"{base}/{n}/address")
        if not label:
            # hciX/device is the USB interface; its parent is the USB device
            usb = os.path.dirname(os.path.realpath(f"{base}/{n}/device"))
            words = [w for w in (read(f"{usb}/manufacturer"), read(f"{usb}/product")) if w]
            vid, pid = read(f"{usb}/idVendor"), read(f"{usb}/idProduct")
            if words:
                label = " ".join(dict.fromkeys(words))   # drop a repeated vendor name
            elif vid:
                label = f"{USB_VENDORS.get(vid, 'USB')} {vid}:{pid}"
        out.append((n, label))
    return out


USB_VENDORS = {"8087": "Intel", "8086": "Intel", "0a12": "CSR", "0bda": "Realtek", "2357": "TP-Link",
               "0cf3": "Qualcomm", "04ca": "Lite-On", "0489": "Foxconn", "13d3": "IMC", "1286": "Marvell",
               "0b05": "ASUS", "050d": "Belkin", "0e8d": "MediaTek", "413c": "Dell", "046d": "Logitech"}


# ---------------------------------------------------------------------------
# Input: MCUboot images
# ---------------------------------------------------------------------------
class Image:
    """A signed MCUboot image; `index` is the MCUboot image number (0 app, 1 net)."""

    def __init__(self, name, data):
        self.name, self.data = name, data
        if len(data) < 32 or struct.unpack_from("<I", data)[0] != MCUBOOT_MAGIC:
            raise OtaError(f"{name}: not an MCUboot image")
        hdr_size, _, img_size = struct.unpack_from("<HHI", data, 8)

        # The two cores are told apart by where their reset vector points.
        reset = struct.unpack_from("<I", data, hdr_size + 4)[0]
        if reset < 0x00100000:
            self.index = 0
        elif 0x01000000 <= reset < 0x01040000:
            self.index = 1
        else:
            raise OtaError(f"{name}: reset vector {reset:#x} is not in either nRF5340 core")

        # The image hash MCUboot and mcumgr identify the image by is in the TLV trailer.
        self.hash = None
        off = hdr_size + img_size
        while off + 4 <= len(data):
            magic, total = struct.unpack_from("<HH", data, off)
            if magic not in TLV_MAGICS:
                break
            pos, off = off + 4, off + total
            while pos + 4 <= off:
                kind, length = struct.unpack_from("<HH", data, pos)
                if kind == TLV_SHA256:
                    self.hash = bytes(data[pos + 4:pos + 4 + length])
                pos += 4 + length
        if off != len(data) or self.hash is None or len(self.hash) != 32:
            raise OtaError(f"{name}: truncated or malformed MCUboot image")
        if len(data) > SLOT_SIZE[self.index]:
            raise OtaError(f"{name}: too large for the {CORE[self.index]} slot")


def load_images(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:4] == struct.pack("<I", MCUBOOT_MAGIC):
        blobs = [(os.path.basename(path), raw)]
    elif zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            blobs = [(n, z.read(n)) for n in z.namelist() if n.endswith(".bin")]
    else:
        raise OtaError(f"{path}: neither an MCUboot .bin nor an update .zip")
    images = sorted((Image(n, d) for n, d in blobs), key=lambda i: i.index)
    if not images or len({i.index for i in images}) != len(images):
        raise OtaError(f"{path}: expected one app core and/or one net core image")
    return images


# ---------------------------------------------------------------------------
# BLE link to one arm, speaking SMP
# ---------------------------------------------------------------------------
class Link:
    def __init__(self, address, name):
        self.address, self.name = address, name
        self.client = None
        self.seq = 0
        self.rx = bytearray()
        self.rx_event = asyncio.Event()
        self.last_keepalive = 0.0

    async def connect(self, wait=60.0):
        """(Re)connect, retrying until the arm has been unreachable for `wait` seconds."""
        deadline = time.monotonic() + wait
        while True:
            try:
                device = await BleakScanner.find_device_by_address(self.address, timeout=10.0,
                                                                   **adapter_kw())
                if device is None:
                    raise OtaError("not advertising")
                self.client = BleakClient(device, disconnected_callback=lambda _: self.rx_event.set(),
                                          **adapter_kw())
                await self.client.connect()
                self.smp = self.client.services.get_characteristic(SMP_CHAR)
                if self.smp is None:
                    raise OtaError("no SMP service")
                await self.client.start_notify(self.smp, self._on_notify)
                self.last_keepalive = time.monotonic()
                return
            except Exception as e:
                await self.close()
                if time.monotonic() > deadline:
                    raise OtaError(f"cannot connect to {self.name}: {e!r} (an arm only accepts a "
                                   f"host other than its paired phone while it sits in the "
                                   f"case with the lid open)") from None
                await asyncio.sleep(2.0)

    async def close(self):
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    def _on_notify(self, _char, data):
        self.rx += data
        self.rx_event.set()

    async def request(self, op, group, cmd, payload, timeout=10.0):
        """One SMP request/response.  Raises OtaError if the firmware answers with
        an error, anything else (timeout, BLE error) if the link failed."""
        self.seq = (self.seq + 1) & 0xFF
        body = cbor2.dumps(payload)
        frame = struct.pack(">BBHHBB", op, 0, len(body), group, self.seq, cmd) + body
        self.rx.clear()
        step = self.smp.max_write_without_response_size
        for i in range(0, len(frame), step):
            await self.client.write_gatt_char(self.smp, frame[i:i + step], response=False)

        deadline = time.monotonic() + timeout
        while True:
            # The response arrives as notifications: 8-byte header (length at
            # bytes 2-3, sequence number at byte 6), then the CBOR body.
            if len(self.rx) >= 8:
                end = 8 + struct.unpack_from(">H", self.rx, 2)[0]
                if len(self.rx) >= end:
                    seq, body = self.rx[6], bytes(self.rx[8:end])
                    del self.rx[:end]
                    if seq != self.seq:
                        continue  # stale response to an earlier request
                    rsp = cbor2.loads(body) if body else {}
                    if rsp.get("rc") or "err" in rsp:
                        raise OtaError(f"{self.name}: SMP group {group} command {cmd} "
                                       f"rejected: {rsp.get('err', rsp.get('rc'))}")
                    return rsp
            if not self.client.is_connected:
                raise ConnectionError("link dropped")
            self.rx_event.clear()
            await asyncio.wait_for(self.rx_event.wait(), deadline - time.monotonic())

    async def image_state(self):
        """{(image, slot): slot info} for every slot holding a valid image."""
        rsp = await self.request(OP_READ, GROUP_IMAGE, IMG_STATE, {})
        return {(s.get("image", 0), s["slot"]): s for s in rsp.get("images", [])}

    async def keepalive(self):
        """The firmware drops a link that has been silent on the command channel
        for 30 s, so send the app's heartbeat packet there every 10 s."""
        if time.monotonic() - self.last_keepalive < 10.0:
            return
        self.last_keepalive = time.monotonic()
        try:
            await self.client.write_gatt_char(NUS_RX_CHAR, bytes([0x25, 6, 0, 0, 4, 0]),
                                              response=False)
        except Exception:
            pass  # upload() reconnects and resumes if the link does drop


# ---------------------------------------------------------------------------
# The update
# ---------------------------------------------------------------------------
async def upload(link, img):
    """Stream `img` into its secondary slot, resuming across link drops."""
    total = len(img.data)
    sha = hashlib.sha256(img.data).digest()
    drops = 0
    off = 0
    while off < total:
        req = {"off": off, "data": img.data[off:off + CHUNK]}
        if off == 0:
            req.update(image=img.index, len=total, sha=sha)
        try:
            await link.keepalive()
            rsp = await link.request(OP_WRITE, GROUP_IMAGE, IMG_UPLOAD, req, timeout=40.0)
        except OtaError:
            raise
        except Exception as e:
            drops += 1
            if drops > 20:
                raise OtaError(f"{link.name}: upload keeps failing ({e!r})") from None
            log(f"\n  link lost at {off}/{total}, reconnecting")
            await link.close()
            await link.connect()
            # A first-chunk request carrying the same sha makes the firmware
            # answer with the offset it already holds instead of starting over.
            off = 0
            continue
        new_off = rsp.get("off", 0)
        if new_off <= off:
            raise OtaError(f"{link.name}: upload stalled at {off}/{total}")
        off = new_off
        print(f"\r  {CORE[img.index]}: {off}/{total} ({100 * off // total}%)", end="", flush=True)
    print()


async def flash_arm(address, name, images):
    log(f"\n== {name} ==")
    link = Link(address, name)
    await link.connect()
    try:
        slots = await link.image_state()
        todo = []
        for img in images:
            if img.index == 0 and slots.get((0, 0), {}).get("hash") == img.hash:
                log(f"  {CORE[0]}: already running {img.hash.hex()[:16]}, skipping")
            else:
                todo.append(img)
        if not todo:
            return

        for img in todo:
            if slots.get((img.index, 1), {}).get("hash") == img.hash:
                log(f"  {CORE[img.index]}: already staged")
            else:
                await upload(link, img)

        # Everything so far only touched the secondary slots.  Check what
        # landed there before arming anything.
        slots = await link.image_state()
        for img in todo:
            if slots.get((img.index, 1), {}).get("hash") != img.hash:
                raise OtaError(f"{name}: {CORE[img.index]} image did not verify after upload")
        for img in todo:
            if not slots[(img.index, 1)].get("pending"):
                await link.request(OP_WRITE, GROUP_IMAGE, IMG_STATE,
                                   {"hash": img.hash, "confirm": False})

        log("  rebooting; do not power off while the bootloader copies the image")
        try:
            await link.request(OP_WRITE, GROUP_OS, OS_RESET, {}, timeout=5.0)
        except Exception:
            try:  # the firmware's own reboot command on the command channel
                await link.client.write_gatt_char(NUS_RX_CHAR, b"#r", response=False)
            except Exception:
                pass
    finally:
        await link.close()

    await asyncio.sleep(5.0)
    await link.connect(wait=180.0)
    try:
        slots = await link.image_state()
    finally:
        await link.close()
    for img in todo:
        if img.index == 0:
            ok = slots.get((0, 0), {}).get("hash") == img.hash
        else:
            # The running net core image cannot be read back over SMP; the
            # bootloader invalidates the staged copy once it has installed it.
            ok = slots.get((1, 1), {}).get("hash") != img.hash
        if not ok:
            raise OtaError(f"{name}: {CORE[img.index]} image was not installed "
                           f"(arm did not reboot, or the bootloader rejected it)")
        log(f"  {CORE[img.index]}: installed {img.hash.hex()[:16]}")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def select_arms(found):
    """The G1 arms among scan results as (name, address), left before right."""
    arms = sorted(((d.name, d.address) for d in found if (d.name or "").startswith(NAME_PREFIX)),
                  key=lambda a: ("_L_" not in a[0], a[0]))
    return arms


async def find_arms():
    log("scanning for G1 arms" + (f" on {ADAPTER}" if ADAPTER else ""))
    arms = select_arms(await BleakScanner.discover(timeout=10.0, **adapter_kw()))
    if not arms:
        raise OtaError("no Even G1 found (out of the case, lid open, Even app closed?)")
    log("found: " + ", ".join(name for name, _ in arms))
    return arms


async def main(path):
    images = load_images(path)
    for img in images:
        log(f"{img.name}: {CORE[img.index]}, {len(img.data)} bytes, hash {img.hash.hex()[:16]}")
    for name, address in await find_arms():
        await flash_arm(address, name, images)
    log("\ndone")


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="update .zip / MCUboot .bin")
    ap.add_argument("--adapter", help="Bluetooth adapter to use, e.g. hci1 (Linux; default: the system's)")
    return ap.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    ADAPTER = args.adapter or None
    try:
        asyncio.run(main(args.path))
    except OtaError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("\ninterrupted")
