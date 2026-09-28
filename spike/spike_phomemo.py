#!/usr/bin/env python3
"""
Milestone 0 spike: print one test label on a Phomemo D30 over Bluetooth.

THROWAWAY CODE. Note: our "D30" turned out to be a PHOMEMO D30, not a
Niimbot D30 - different company, different protocol. niimprint is useless here.

The Phomemo protocol is tiny (ESC/POS raster), so this script has no printer
library at all. The byte sequences come from three independent projects that
agree with each other:
  - polskafan/phomemo_d30 (MIT)               - Classic RFCOMM, channel 1
  - odensc/phomemo-d30-web-bluetooth (Apache-2.0) - BLE service FF00, char FF02
  - daehyeok/d30-printer (AGPL, only read, nothing copied)

What it answers:
  1. Does the D30 accept Classic RFCOMM, BLE, or both? (report says TRANSPORT)
  2. Does the plain ESC/POS raster sequence print correctly (size, position,
     orientation) on our labels?
  3. What does the printer answer to the 1F 11 xx queries the phone app sends
     (battery, firmware...)? Logged as hex so we can decode later.

Usage (on the Pi):
    python spike_phomemo.py --address XX:XX:XX:XX:XX:XX
    python spike_phomemo.py --scan            # find it by BLE name "D30" (needs bleak)
    python spike_phomemo.py --dry-run
"""

import argparse
import asyncio
import errno
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"

PX_PER_MM = 8           # 203 dpi
PRINTHEAD_PX = 96       # 12 bytes per raster row
BLE_SERVICE = "0000ff00-0000-1000-8000-00805f9b34fb"
BLE_WRITE_CHAR = "0000ff02-0000-1000-8000-00805f9b34fb"
BLE_CHUNK = 128         # odensc: "seems to work best with 128 bytes"

# Queries the Android app sends on connect (sniffed by polskafan). We don't
# know exactly what each one means yet; we send them one at a time and log
# whatever comes back.
INIT_QUERIES = ["1f1138", "1f11121f1113", "1f1109", "1f1111", "1f1119", "1f1107", "1f110a1f110202"]

END_OF_JOB = bytes([0x1B, 0x64, 0x00])  # ESC d 0 (odensc sends this last)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def describe_os_error(exc: OSError) -> str:
    name = errno.errorcode.get(exc.errno, "?") if exc.errno else type(exc).__name__
    return f"{name} ({exc})"


def bluetoothctl_info(address: str) -> str:
    try:
        result = subprocess.run(["bluetoothctl", "info", address], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or result.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(could not run bluetoothctl: {exc})"


# ---------------------------------------------------------------------------
# Transports: both offer write(data) and read_available(seconds) -> bytes
# ---------------------------------------------------------------------------
class ClassicLink:
    """Bluetooth Classic RFCOMM through the stdlib socket module."""

    def __init__(self, address: str, channel: int = 1, connect_timeout: float = 10):
        self._sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        self._sock.settimeout(connect_timeout)
        try:
            self._sock.connect((address, channel))
        except BaseException:
            self._sock.close()
            raise
        # SO_LINGER: make close() wait (up to 10 s) for queued data to be sent
        # instead of discarding it.
        import struct

        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 10))
        self.description = f"Bluetooth Classic RFCOMM (channel {channel})"

    # Set from --classic-chunk / --classic-delay. 0 = send everything at once.
    chunk_size = 0
    chunk_delay = 0.0

    def write(self, data: bytes) -> None:
        """
        Over BLE the printer acknowledges every 128-byte write, which paces us.
        Classic has no such pacing, and sending the whole job at once did NOT
        print. So optionally send in small chunks with a pause in between.
        """
        self._sock.settimeout(10)
        if not self.chunk_size:
            self._sock.sendall(data)
            return
        for i in range(0, len(data), self.chunk_size):
            self._sock.sendall(data[i : i + self.chunk_size])
            time.sleep(self.chunk_delay)

    def read_available(self, seconds: float) -> bytes:
        """Collect whatever arrives within `seconds`."""
        deadline = time.monotonic() + seconds
        buf = bytearray()
        while (remaining := deadline - time.monotonic()) > 0:
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(256)
            except socket.timeout:
                break
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    def close(self) -> None:
        self._sock.close()


class BleLink:
    """
    BLE GATT through bleak. bleak is asyncio-only, so an event loop runs on a
    background thread and we block on each coroutine (like .GetAwaiter().GetResult()).
    """

    def __init__(self, address: str, connect_timeout: float = 20):
        from bleak import BleakClient  # only needed for BLE

        self._rx = bytearray()
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._client = BleakClient(address, timeout=connect_timeout)
        try:
            self._run(self._connect(), connect_timeout + 10)
        except BaseException:
            self.close()
            raise

    def _run(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _connect(self) -> None:
        await self._client.connect()
        services = {s.uuid.lower(): s for s in self._client.services}
        lines = []
        for s in self._client.services:
            for c in s.characteristics:
                lines.append(f"    service {s.uuid} char {c.uuid} {sorted(c.properties)}")
        self.gatt_dump = "\n".join(lines)
        if BLE_SERVICE not in services:
            raise RuntimeError("connected, but no FF00 service - not a Phomemo D30?\n" + self.gatt_dump)
        self._write_char = services[BLE_SERVICE].get_characteristic(BLE_WRITE_CHAR)
        if self._write_char is None:
            raise RuntimeError("FF00 service has no FF02 characteristic\n" + self.gatt_dump)
        # Subscribe to every notify characteristic in FF00 (usually FF03) so we
        # see whatever the printer answers.
        for c in services[BLE_SERVICE].characteristics:
            if "notify" in c.properties:
                await self._client.start_notify(c, self._on_notify)
        self.description = f"BLE GATT (service FF00, write FF02, max write {self._write_char.max_write_without_response_size})"

    def _on_notify(self, _sender, data: bytearray) -> None:
        with self._lock:
            self._rx.extend(data)

    def write(self, data: bytes) -> None:
        for i in range(0, len(data), BLE_CHUNK):
            self._run(self._client.write_gatt_char(self._write_char, data[i : i + BLE_CHUNK], response=True), 10)

    def read_available(self, seconds: float) -> bytes:
        time.sleep(seconds)
        with self._lock:
            out = bytes(self._rx)
            self._rx.clear()
        return out

    def close(self) -> None:
        try:
            if self._client.is_connected:
                self._run(self._client.disconnect(), 10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


def ble_scan_for_d30(seconds: float) -> str | None:
    """Find a BLE device advertising the name 'D30' (what daehyeok/d30-printer looks for)."""
    from bleak import BleakScanner

    async def scan():
        found = await BleakScanner.discover(timeout=seconds, return_adv=True)
        hits = []
        for device, adv in found.values():
            name = adv.local_name or device.name or ""
            if "D30" in name.upper() or BLE_SERVICE in [u.lower() for u in adv.service_uuids]:
                hits.append((adv.rssi, device.address, name, adv.service_uuids))
        return sorted(hits, reverse=True)

    hits = asyncio.run(scan())
    for rssi, addr, name, uuids in hits:
        log(f"  found {addr} name={name!r} rssi={rssi} uuids={uuids}")
    return hits[0][1] if hits else None


def connect(address: str, mode: str, channel: int, report: dict):
    attempts = (["classic"] if mode in ("auto", "classic") else []) + (["ble"] if mode in ("auto", "ble") else [])
    for kind in attempts:
        log(f"trying {kind.upper()} on {address} ...")
        t0 = time.monotonic()
        try:
            link = ClassicLink(address, channel) if kind == "classic" else BleLink(address)
        except ImportError:
            log("  bleak not installed - skipping BLE")
            report[f"try_{kind}"] = "skipped: bleak not installed"
            continue
        except OSError as exc:
            log(f"  failed: {describe_os_error(exc)}")
            report[f"try_{kind}"] = f"FAIL {describe_os_error(exc)}"
            continue
        except Exception as exc:
            log(f"  failed: {type(exc).__name__}: {exc}")
            report[f"try_{kind}"] = f"FAIL {type(exc).__name__}: {exc}"
            continue
        ms = round((time.monotonic() - t0) * 1000)
        log(f"  CONNECTED via {link.description} in {ms} ms")
        if isinstance(link, BleLink):
            print(link.gatt_dump)
        report[f"try_{kind}"] = "connected"
        report["TRANSPORT"] = link.description
        report["connect_ms"] = ms
        return link, kind
    return None, None


# ---------------------------------------------------------------------------
# Image + ESC/POS encoding
# ---------------------------------------------------------------------------
def load_font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",  # Arch / Manjaro
        "C:/Windows/Fonts/arialbd.ttf",
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def fit_font(draw: ImageDraw.ImageDraw, text: str, max_w: int, max_h: int) -> ImageFont.ImageFont:
    for size in range(max_h, 4, -1):
        font = load_font(size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= max_w and bottom - top <= max_h:
            return font
    return load_font(5)


def make_test_image(width_mm: int, length_mm: int, subtitle: str) -> Image.Image:
    """Drawn as you read the label (landscape: length x width). Rotated later."""
    w, h = length_mm * PX_PER_MM, width_mm * PX_PER_MM
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([2, 2, w - 3, h - 3], outline=0, width=2)  # shows the printable area
    draw.polygon([(8, 8), (24, 8), (8, 24)], fill=0)          # shows orientation
    inner_w, inner_h = w - 16, h - 12
    title = "labelpi D30"
    font1 = fit_font(draw, title, inner_w - 24, int(inner_h * 0.5))
    font2 = fit_font(draw, subtitle, inner_w, int(inner_h * 0.3))
    draw.text((w // 2, 6 + int(inner_h * 0.3)), title, font=font1, fill=0, anchor="mm")
    draw.text((w // 2, 6 + int(inner_h * 0.8)), subtitle, font=font2, fill=0, anchor="mm")
    return img


def make_ruler_image(width_mm: int, length_mm: int) -> Image.Image:
    """
    A ruler along the label, to measure where printing lands on the label.
    Tick every 1 mm, longer tick every 5 mm with its number, a solid block at
    0 mm (the start of the image) and lines along both long edges (to check
    the width). Compare the numbers with the label's physical edges.
    """
    w, h = length_mm * PX_PER_MM, width_mm * PX_PER_MM
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w - 1, 1], fill=0)          # top edge line
    draw.rectangle([0, h - 2, w - 1, h - 1], fill=0)  # bottom edge line
    draw.rectangle([0, 0, 5, h - 1], fill=0)          # start-of-image block
    font = load_font(h // 4)
    for mm in range(1, length_mm + 1):
        x = mm * PX_PER_MM - 1
        tick = h // 2 if mm % 10 == 0 else h // 3 if mm % 5 == 0 else h // 6
        draw.line([(x, 0), (x, tick)], fill=0, width=2 if mm % 5 == 0 else 1)
        if mm % 10 == 0:
            anchor = "rb" if mm == length_mm else "mb"  # keep the last number on the label
            draw.text((x, h - 6), str(mm), font=font, fill=0, anchor=anchor)
    return img


def encode_job(image: Image.Image) -> bytes:
    """
    Header + raster + end, as in polskafan (header) and odensc (end):
      1F 11 24 00          - sent by the phone app before every image (meaning unknown)
      1B 40                - ESC @, reset
      1D 76 30 00 xL xH yL yH - GS v 0: raster image, x = bytes per row, y = rows
      <rows x bytes>       - 1 bit per pixel, MSB = leftmost, 1 = black dot
      1B 64 00             - ESC d 0
    """
    img = image.convert("1", dither=Image.Dither.NONE)
    width_bytes = (img.width + 7) // 8
    rows = img.height
    header = bytes([0x1F, 0x11, 0x24, 0x00, 0x1B, 0x40, 0x1D, 0x76, 0x30, 0x00,
                    width_bytes & 0xFF, width_bytes >> 8, rows & 0xFF, rows >> 8])
    # Pillow's "1" mode stores white as 1; the printer wants black as 1. So invert.
    raster = bytes(b ^ 0xFF for b in img.tobytes())
    return header + raster + END_OF_JOB


# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--address", help="Bluetooth MAC of the D30")
    p.add_argument("--scan", action="store_true", help="find the D30 by BLE name first (needs bleak)")
    p.add_argument("--transport", choices=["auto", "classic", "ble"], default="auto")
    p.add_argument("--channel", type=int, default=1, help="RFCOMM channel (default 1)")
    p.add_argument("--label", default="12x40", help="label WIDTHxLENGTH in mm (default 12x40)")
    p.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], default=270,
                   help="clockwise rotation before sending (default 270, as polskafan)")
    p.add_argument("--no-init", action="store_true", help="skip the 1F 11 xx query packets")
    p.add_argument("--calibrate", action="store_true", help="print a mm ruler instead of the test label")
    p.add_argument("--classic-chunk", type=int, default=0, help="Classic: send in chunks of N bytes (0 = all at once)")
    p.add_argument("--classic-delay", type=float, default=0.05, help="Classic: seconds between chunks")
    p.add_argument("--info-only", action="store_true", help="connect and query, don't print")
    p.add_argument("--dry-run", action="store_true", help="only write the PNG, don't connect")
    args = p.parse_args()

    ClassicLink.chunk_size = args.classic_chunk
    ClassicLink.chunk_delay = args.classic_delay
    OUT_DIR.mkdir(exist_ok=True)
    width_mm, length_mm = (int(x) for x in args.label.lower().split("x"))
    report: dict[str, object] = {"label": args.label, "rotate": args.rotate}

    print("=" * 70)
    print(f"python   : {sys.version.split()[0]} on {sys.platform}")
    try:
        import bleak  # noqa: F401

        print("bleak    : installed")
    except ImportError:
        print("bleak    : not installed (needed for BLE and --scan)")

    address = args.address
    if not args.dry_run and not address:
        if not args.scan:
            p.error("--address or --scan is required unless --dry-run")
        log("BLE scan for 'D30' (15 s) ...")
        address = ble_scan_for_d30(15)
        if address is None:
            report["TRANSPORT"] = "NONE - D30 not found by BLE scan"
            return finish(report, ok=False)
    if address:
        print(f"--- bluetoothctl info {address} ---")
        print(bluetoothctl_info(address))
    print("=" * 70)

    link = None
    try:
        kind = "dry run"
        if not args.dry_run:
            link, kind = connect(address, args.transport, args.channel, report)
            if link is None:
                report["TRANSPORT"] = "NONE - could not connect over Classic or BLE"
                return finish(report, ok=False)
            report["connected_address"] = address
            if kind == "classic" and args.classic_chunk:
                kind = f"classic {args.classic_chunk}B"  # printed on the label, so we can tell tests apart
            if not args.no_init:
                log("sending the app's init queries ...")
                for query in INIT_QUERIES:
                    link.write(bytes.fromhex(query))
                    answer = link.read_available(0.6)
                    log(f"  {query} -> {answer.hex(' ') or '(no answer)'}")
                    report[f"q_{query}"] = answer.hex(" ") or "-"

        if args.calibrate:
            image = make_ruler_image(width_mm, length_mm)
        else:
            image = make_test_image(width_mm, length_mm, f"phomemo / {kind}")
        if args.rotate:
            image = image.rotate(-args.rotate, expand=True)  # PIL rotates anticlockwise
        image.save(OUT_DIR / f"phomemo_{args.label}.png")
        log(f"image {image.width}x{image.height} px (printhead {PRINTHEAD_PX} px)")
        if image.width > PRINTHEAD_PX:
            raise RuntimeError(f"image is {image.width} px wide, printhead is {PRINTHEAD_PX}")
        report["image_px"] = f"{image.width}x{image.height}"

        if args.dry_run or args.info_only:
            return finish(report, ok=True)

        job = encode_job(image)
        log(f"sending {len(job)} bytes ...")
        t0 = time.monotonic()
        link.write(job)
        # Give the printer time to take everything in before we hang up.
        # Nothing tells us it's finished, so we just listen for a few seconds.
        after = link.read_available(4)
        report["print_send_ms"] = round((time.monotonic() - t0) * 1000) - 4000
        report["after_print_bytes"] = after.hex(" ") or "-"
        log(f"after print, printer sent: {after.hex(' ') or '(nothing)'}")
        report["print_result"] = "sent (check the label!)"
        return finish(report, ok=True)
    except OSError as exc:
        report["print_result"] = f"FAIL {describe_os_error(exc)}"
    except Exception as exc:
        report["print_result"] = f"FAIL {type(exc).__name__}: {exc}"
    finally:
        if link is not None:
            link.close()
            log("disconnected")
    return finish(report, ok=False)


def finish(report: dict, ok: bool) -> int:
    print()
    print("=== SPIKE REPORT: phomemo ===")
    for key, value in report.items():
        print(f"{key}: {value}")
    print("=== END REPORT ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
