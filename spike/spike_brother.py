#!/usr/bin/env python3
"""
Milestone 0 spike: print one test label on a Brother PT-P300BT over Bluetooth.

THROWAWAY CODE. It answers three questions and is then deleted:
  1. Can a plain stdlib RFCOMM socket (no `rfcomm bind`, no /dev/rfcomm0)
     drive Ircama's PT-P300BT code?
  2. How long do connect and print take on a Pi Zero W?
  3. How many pixel rows actually print on each tape width? (--calibrate)

Ircama's code is NOT copied into this repo (his repository has no licence, so
we may not redistribute it). Instead we import it from a local git clone,
by default spike/libs/PT-P300BT - see README.md.

Usage (on the Pi):
    python spike_brother.py --address AA:BB:CC:DD:EE:FF
    python spike_brother.py --address AA:BB:CC:DD:EE:FF --calibrate
    python spike_brother.py --dry-run          # just write PNGs, no printer
"""

import argparse
import errno
import io
import os
import socket
import subprocess
import sys
import time
import types
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"

# The PT-P300BT always receives a 128-pixel-wide raster (16 bytes per line),
# whatever tape is loaded. Only a band in the middle actually reaches the tape.
RASTER_PX = 128

# Printable band per tape width, as used by Ircama's printlabel.py
# ("12 mm tape offers a 9 mm printable band (64 px)", smaller tapes scale
# linearly). These are exactly the numbers --calibrate is meant to check.
BAND_PX_BY_TAPE_MM = {12: 64, 9: 48, 6: 32, 3: 16}  # 3 = 3.5 mm tape

# Along the tape, one raster line is 0.149 mm (Ircama) -> ~170 dpi, not 180.
MM_PER_LINE = 0.149


# ---------------------------------------------------------------------------
# Transport: a tiny serial.Serial look-alike on top of a stdlib RFCOMM socket.
# ---------------------------------------------------------------------------
class RfcommSerial:
    """
    Wraps a Bluetooth RFCOMM socket so it looks like pyserial's Serial object.

    Ircama's labelmaker.py only uses: write(bytes), read(n), the `timeout`
    attribute (it sets it to 1 while waiting for the print to finish),
    reset_input_buffer() and close().

    Important difference from a raw socket: pyserial's read(n) keeps reading
    until it has n bytes OR the timeout expires. socket.recv(n) returns as
    soon as *any* bytes arrive, which would break the 32-byte status read.
    So read() below loops. (Ircama's own socket adapter in native/btcommon.py
    does NOT loop - one reason we don't use it.)
    """

    def __init__(self, address: str, channel: int, connect_timeout: float, timeout: float):
        self.timeout = timeout
        self._sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        self._sock.settimeout(connect_timeout)
        try:
            self._sock.connect((address, channel))
        except BaseException:
            self._sock.close()
            raise

    def write(self, data: bytes) -> int:
        self._sock.settimeout(self.timeout)
        self._sock.sendall(bytes(data))
        return len(data)

    def read(self, n: int = 1) -> bytes:
        deadline = time.monotonic() + (self.timeout or 0)
        buf = bytearray()
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(n - len(buf))
            except socket.timeout:
                break
            if not chunk:  # peer closed the connection
                break
            buf.extend(chunk)
        return bytes(buf)

    def reset_input_buffer(self) -> None:
        """Throw away anything the printer sent that nobody read yet."""
        self._sock.setblocking(False)
        try:
            while self._sock.recv(1024):
                pass
        except (BlockingIOError, OSError):
            pass
        finally:
            self._sock.setblocking(True)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def describe_os_error(exc: OSError) -> str:
    """Turn e.g. errno 112 into 'EHOSTDOWN (Host is down)' - easier to diagnose."""
    name = errno.errorcode.get(exc.errno, "?") if exc.errno else type(exc).__name__
    return f"{name} ({exc})"


def import_ircama(lib_path: Path):
    """Import labelmaker/ptcbp/ptstatus from Ircama's clone."""
    if not (lib_path / "labelmaker.py").exists():
        sys.exit(f"Ircama's code not found in {lib_path} - see README.md step 3.")
    sys.path.insert(0, str(lib_path))
    import labelmaker  # noqa: E402  (imports ptcbp, ptstatus, labelmaker_encode)
    import labelmaker_encode
    import ptcbp
    import ptstatus

    return labelmaker, labelmaker_encode, ptcbp, ptstatus


def bluetoothctl_info(address: str) -> None:
    """Print `bluetoothctl info` so we can see Paired/Trusted and the UUIDs."""
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", address], capture_output=True, text=True, timeout=10
        )
        print(result.stdout.strip() or result.stderr.strip())
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"(could not run bluetoothctl: {exc})")


def load_font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",  # Arch / Manjaro
        "C:/Windows/Fonts/arialbd.ttf",  # lets --dry-run work on a dev PC too
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)  # Pillow >= 10.1


def fit_font(draw: ImageDraw.ImageDraw, text: str, max_w: int, max_h: int) -> ImageFont.ImageFont:
    """Largest font size (simple linear search downwards) where `text` fits the box."""
    for size in range(max_h, 4, -1):
        font = load_font(size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= max_w and bottom - top <= max_h:
            return font
    return load_font(5)


# ---------------------------------------------------------------------------
# Test images. Drawn "as you read the label": x = along the tape (length),
# y = across the tape. Black on white. Ircama's read_png() rotates, mirrors,
# inverts and centres it in the 128-px raster for us.
# ---------------------------------------------------------------------------
def make_test_image(band_px: int, tape_mm: int) -> Image.Image:
    length_px = 420  # ~63 mm of tape
    img = Image.new("L", (length_px, band_px), 255)
    draw = ImageDraw.Draw(img)

    # 2-px border exactly on the edges of the band: if all four sides print,
    # the band fits on the tape. If top/bottom are cut off, the band is too big.
    draw.rectangle([0, 0, length_px - 1, band_px - 1], outline=0, width=2)

    line1 = "labelpi PT-P300BT"
    line2 = f"{tape_mm} mm tape - band {band_px} px"
    inner_h = band_px - 8
    font1 = fit_font(draw, line1, length_px - 16, int(inner_h * 0.6))
    font2 = fit_font(draw, line2, length_px - 16, int(inner_h * 0.35))
    draw.text((length_px // 2, 4 + int(inner_h * 0.3)), line1, font=font1, fill=0, anchor="mm")
    draw.text((length_px // 2, 4 + int(inner_h * 0.8)), line2, font=font2, fill=0, anchor="mm")
    return img


def make_calibration_image() -> Image.Image:
    """
    A staircase that covers ALL 128 raster rows, to find the printable band.

    Step k (k = 0..31) is a 20-px-long black block covering rows 4k..4k+3,
    and every 4th step (0, 4, 8, ...) is drawn twice as long so you can count.
    On the printed tape: note the first and last step you can see.
    Printable rows = (last - first + 1) * 4, starting at row first * 4.
    A solid bar at the end shows the whole printable height in one go.
    """
    step_len = 20  # ~3 mm of tape per step, so the steps are easy to count
    steps = RASTER_PX // 4
    length_px = steps * step_len + 60
    img = Image.new("L", (length_px, RASTER_PX), 255)
    draw = ImageDraw.Draw(img)
    for k in range(steps):
        x0 = k * step_len
        x1 = x0 + (step_len * 2 if k % 4 == 0 else step_len) - 1
        draw.rectangle([x0, 4 * k, x1, 4 * k + 3], fill=0)
    # Solid bar over all 128 rows.
    draw.rectangle([length_px - 40, 0, length_px - 1, RASTER_PX - 1], fill=0)
    return img


def raster_preview(data: bytes) -> Image.Image:
    """What actually goes over the wire: 128 px wide, one row per raster line.
    (Bits set = black dot. We invert for viewing.)"""
    lines = len(data) // 16
    raw = Image.frombytes("1", (RASTER_PX, lines), data)
    return Image.eval(raw.convert("L"), lambda p: 255 - p)


# ---------------------------------------------------------------------------
# Printer conversation
# ---------------------------------------------------------------------------
def query_status(ser, labelmaker, ptcbp, ptstatus):
    """Same handshake as labelmaker.do_print_job: reset, ask for 32-byte status."""
    for attempt in range(1, 4):
        labelmaker.reset_printer(ser)
        ser.reset_input_buffer()
        ser.write(ptcbp.serialize_control("get_status"))
        resp = ser.read(32)
        if len(resp) == 32:
            return ptstatus.unpack_status(resp)
        log(f"status attempt {attempt}: got {len(resp)} bytes, retrying")
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--address", help="Bluetooth MAC of the PT-P300BT")
    p.add_argument("--channel", type=int, default=1, help="RFCOMM channel (default 1 = SPP)")
    p.add_argument("--lib", type=Path, default=HERE / "libs" / "PT-P300BT", help="path to Ircama's clone")
    p.add_argument("--calibrate", action="store_true", help="print the 128-row staircase instead of the test label")
    p.add_argument("--band-px", type=int, help="override the printable band height (default: from tape width)")
    p.add_argument("--tape-mm", type=int, choices=sorted(BAND_PX_BY_TAPE_MM), help="assume this tape (--dry-run)")
    p.add_argument("--repeat", type=int, default=1, help="print N times, reconnecting each time")
    p.add_argument("--dry-run", action="store_true", help="only write PNGs to spike/out/, don't connect")
    args = p.parse_args()

    if not args.dry_run and not args.address:
        p.error("--address is required unless --dry-run")

    labelmaker, labelmaker_encode, ptcbp, ptstatus = import_ircama(args.lib)
    OUT_DIR.mkdir(exist_ok=True)

    print("=" * 70)
    print(f"python   : {sys.version.split()[0]} on {sys.platform}")
    print(f"AF_BLUETOOTH available: {hasattr(socket, 'AF_BLUETOOTH')}")
    if args.address:
        print(f"--- bluetoothctl info {args.address} ---")
        bluetoothctl_info(args.address)
    print("=" * 70)

    report: dict[str, object] = {"transport": f"stdlib RFCOMM socket, channel {args.channel}"}
    results = []

    for run in range(1, args.repeat + 1):
        if args.repeat > 1:
            log(f"===== run {run}/{args.repeat} =====")
        ser = None
        status = None
        t_start = time.monotonic()
        try:
            if not args.dry_run:
                log(f"connecting to {args.address} channel {args.channel} ...")
                ser = RfcommSerial(args.address, args.channel, connect_timeout=15, timeout=5)
                t_conn = time.monotonic() - t_start
                log(f"connected in {t_conn * 1000:.0f} ms")
                report[f"run{run}_connect_ms"] = round(t_conn * 1000)

                status = query_status(ser, labelmaker, ptcbp, ptstatus)
                if status is None:
                    raise RuntimeError("printer never returned a 32-byte status")
                ptstatus.print_status(status)
                report["model_code"] = hex(status.model)
                report["tape_width_mm"] = status.tape_width
                report["tape_type"] = hex(status.tape_type)
                report["error_flags"] = hex(status.err)

            tape_mm = args.tape_mm or (status.tape_width if status else 12)
            # 3.5 mm tape reports as 3 or 4 depending on firmware; default to 64 if unknown.
            band_px = args.band_px or BAND_PX_BY_TAPE_MM.get(tape_mm, 64)
            report["band_px_used"] = "all 128 (calibrate)" if args.calibrate else band_px

            image = make_calibration_image() if args.calibrate else make_test_image(band_px, tape_mm)
            name = "brother_calibrate" if args.calibrate else f"brother_test_{tape_mm}mm"
            image.save(OUT_DIR / f"{name}.png")

            # Ircama's read_png accepts anything Image.open accepts, including a
            # file-like object. transform=True rotates+mirrors, padding=True
            # centres the image in the 128-px raster.
            png = io.BytesIO()
            image.save(png, format="PNG")
            png.seek(0)
            data = labelmaker_encode.read_png(png, transform=True, padding=True, dither=False)
            raster_preview(data).save(OUT_DIR / f"{name}_raster.png")
            lines = len(data) // 16
            log(f"image {image.width}x{image.height}px -> {lines} raster lines "
                f"(~{lines * MM_PER_LINE:.0f} mm of print); PNGs in {OUT_DIR}")

            if args.dry_run:
                results.append("dry-run")
                continue

            # labelmaker.do_print_job wants an argparse-like object.
            job_args = types.SimpleNamespace(
                no_feed=False, auto_cut=False, end_margin=0, nocomp=False, no_print=False
            )
            t_job = time.monotonic()
            try:
                labelmaker.do_print_job(ser, job_args, data)
            except SystemExit:  # do_print_job calls sys.exit(1) on "not ready"
                raise RuntimeError("labelmaker refused to print (see printer status above)")
            job_ms = round((time.monotonic() - t_job) * 1000)
            total_ms = round((time.monotonic() - t_start) * 1000)
            log(f"print job finished in {job_ms} ms (total incl. connect {total_ms} ms)")
            report[f"run{run}_job_ms"] = job_ms
            report[f"run{run}_total_ms"] = total_ms
            results.append("OK")
        except OSError as exc:
            log(f"Bluetooth error: {describe_os_error(exc)}")
            results.append(f"FAIL {describe_os_error(exc)}")
        except Exception as exc:  # spike: report everything, don't hide it
            log(f"FAILED: {type(exc).__name__}: {exc}")
            results.append(f"FAIL {type(exc).__name__}: {exc}")
        finally:
            if ser is not None:
                try:
                    labelmaker.reset_printer(ser)  # same as Ircama's main()
                except OSError:
                    pass
                ser.close()
                log("disconnected")
        if run < args.repeat:
            time.sleep(3)

    report["results"] = ", ".join(results)
    print()
    print("=== SPIKE REPORT: brother ===")
    for key, value in report.items():
        print(f"{key}: {value}")
    print("=== END REPORT ===")
    return 0 if all(r in ("OK", "dry-run") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
