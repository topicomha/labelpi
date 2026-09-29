"""
Brother PT-P300BT backend: Bluetooth Classic RFCOMM + Brother's raster protocol.

Our own implementation (MIT, like the rest of labelpi). The command bytes are
Brother's documented raster protocol ("PTCBP"), and every command used here
was exercised against a real PT-P300BT in the Milestone 0 spike
(docs/SPEC.md section 10).

A print job, start to finish:
    connect RFCOMM channel 1 (the printer must be paired + trusted)
    reset, enter raster mode, ask for the 32-byte status
    refuse if the printer reports an error or the wrong tape is loaded
    send: print settings, then one command per raster line, then "print"
    read status messages until "printing completed"
    reset and disconnect

Raster geometry: the print head always takes 128-pixel lines (16 bytes),
one line per step along the tape. Only a band in the middle reaches the tape
(12 mm tape: 64 px, rows 32-95 - measured). render.py draws the label as you
read it; encode_raster() turns it into those lines.
"""

from __future__ import annotations

import errno
import logging
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from PIL import Image, ImageOps

from labelpi.config import LabelConfig, PrinterConfig
from labelpi.printers.base import Printer, PrinterError, PrinterUnavailable

log = logging.getLogger(__name__)

RFCOMM_CHANNEL = 1
CONNECT_TIMEOUT_S = 15
READ_TIMEOUT_S = 5
PRINT_TIMEOUT_S = 60  # a 40 cm label takes well under this
RASTER_PX = 128
LINE_BYTES = RASTER_PX // 8
STATUS_BYTES = 32

# --- Commands --------------------------------------------------------------
INVALIDATE = b"\x00" * 64  # clears a half-received job out of the printer
INITIALIZE = b"\x1b@"  # ESC @
RASTER_MODE = b"\x1bia\x01"  # ESC i a 1: switch to raster commands
STATUS_REQUEST = b"\x1biS"  # ESC i S: answer with a 32-byte status
COMPRESSION_TIFF = b"M\x02"  # raster lines are PackBits-compressed
ZERO_LINE = b"Z"  # a blank raster line, no data
PRINT_AND_FEED = b"\x1a"  # print the job and feed the tape out

# ESC i z "print information": which fields are valid, plus quality/recovery.
PI_WIDTH, PI_QUALITY, PI_RECOVERY = 0x04, 0x40, 0x80
NO_CHAINING = 0x08  # ESC i K: feed the finished label out (don't wait for another)

# --- Status bytes ------------------------------------------------------------
STATUS_REPLY, STATUS_COMPLETED, STATUS_ERROR, STATUS_POWER_OFF = 0x00, 0x01, 0x02, 0x04

# Bits of the 16-bit error field (status bytes 8-9).
ERROR_FLAGS = {
    0: "replace the tape",
    1: "expansion buffer full",
    2: "communication error",
    3: "communication buffer full",
    4: "cover is open",
    5: "overheated, or cancelled on the printer",
    6: "feed error",
    7: "system error",
    8: "no tape loaded",
    9: "end of tape (label too long)",
    10: "cutter jammed",
    11: "battery low",
    12: "printer in use",
    13: "printer not powered",
    14: "overvoltage",
    15: "fan error",
}


@dataclass(frozen=True)
class Status:
    """The fields we use from the printer's 32-byte status message."""

    model: int
    errors: int
    tape_width_mm: int
    tape_type: int
    tape_length_mm: int
    status_type: int
    phase_type: int
    phase: int

    @classmethod
    def parse(cls, data: bytes) -> Status:
        if len(data) != STATUS_BYTES or data[0] != 0x80:
            raise PrinterError(f"unexpected status message: {data.hex(' ')}")
        return cls(
            model=data[4],
            errors=struct.unpack(">H", data[8:10])[0],
            tape_width_mm=data[10],
            tape_type=data[11],
            tape_length_mm=data[17],
            status_type=data[18],
            phase_type=data[19],
            phase=struct.unpack(">H", data[20:22])[0],
        )

    def error_text(self) -> str:
        """e.g. 'cover is open, no tape loaded'; empty if there are no errors."""
        return ", ".join(text for bit, text in ERROR_FLAGS.items() if self.errors & (1 << bit))

    @property
    def ready(self) -> bool:
        return self.errors == 0 and self.phase_type == 0 and self.phase == 0


# ---------------------------------------------------------------------------
# Encoding (pure functions - easy to test without a printer)
# ---------------------------------------------------------------------------
def packbits(data: bytes) -> bytes:
    """
    PackBits (TIFF) compression, which the printer expects for raster lines.

    Output is a series of chunks: a header byte n, then either
      n in 0..127  -> the next n+1 bytes are copied as-is (a "literal")
      n in 129..255 -> the next single byte is repeated 257-n times (a "run")
    """
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        run = 1
        while i + run < n and run < 128 and data[i + run] == data[i]:
            run += 1
        if run >= 2:
            out.append(257 - run)
            out.append(data[i])
            i += run
            continue
        start = i
        i += 1
        # Extend the literal until two equal bytes start a run, or 128 bytes.
        while i < n and i - start < 128 and not (i + 1 < n and data[i] == data[i + 1]):
            i += 1
        out.append(i - start - 1)
        out += data[start:i]
    return bytes(out)


def raster_lines(image: Image.Image) -> list[bytes]:
    """
    Turn a label image (as you read it: x along the tape, y across it) into
    16-byte raster lines, first line = left edge of the label.

    rotate -90 makes each column of the label a row; the mirror matches how
    the head is wired; padding centres the band in the 128-px line. Printer
    bits are 1 = black, the opposite of Pillow's mode "1", so we invert.
    """
    if image.height > RASTER_PX:
        raise PrinterError(f"label is {image.height} px tall; the print head has {RASTER_PX}")
    rows = ImageOps.mirror(image.convert("L").rotate(-90, expand=True))
    padded = Image.new("L", (RASTER_PX, rows.height), 255)
    padded.paste(rows, ((RASTER_PX - rows.width) // 2, 0))
    bits = ImageOps.invert(padded).convert("1", dither=Image.Dither.NONE).tobytes()
    return [bits[i : i + LINE_BYTES] for i in range(0, len(bits), LINE_BYTES)]


def encode_job(image: Image.Image, status: Status) -> bytes:
    """Everything sent after the status check, ending with 'print and feed'."""
    lines = raster_lines(image)
    job = bytearray(INVALIDATE + INITIALIZE + RASTER_MODE)
    job += b"\x1biz" + struct.pack(
        "<4BI2B",
        PI_WIDTH | PI_QUALITY | PI_RECOVERY,
        status.tape_type,
        status.tape_width_mm,
        status.tape_length_mm,
        len(lines),
        0,  # "follow-up page": no
        0,  # reserved
    )
    job += b"\x1biK" + bytes([NO_CHAINING])  # advanced mode
    job += b"\x1biM" + bytes([0])  # various mode: no auto cut, no mirror
    job += b"\x1bid" + struct.pack("<H", 0)  # end margin (feed) in dots
    job += COMPRESSION_TIFF
    for line in lines:
        if not any(line):
            job += ZERO_LINE
        else:
            data = packbits(line)
            job += b"G" + struct.pack("<H", len(data)) + data
    job += PRINT_AND_FEED
    return bytes(job)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
class Connection(Protocol):
    """The socket methods we use - lets tests pass in a fake printer."""

    def sendall(self, data: bytes) -> None: ...
    def recv(self, size: int) -> bytes: ...
    def settimeout(self, timeout: float | None) -> None: ...
    def close(self) -> None: ...


def rfcomm_connect(address: str, channel: int, timeout: float) -> Connection:
    """Open a Bluetooth Classic RFCOMM socket (stdlib; no `rfcomm bind` needed)."""
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    sock.settimeout(timeout)
    try:
        sock.connect((address, channel))
    except BaseException:
        sock.close()
        raise
    return sock


def read_exact(conn: Connection, size: int, timeout: float) -> bytes:
    """
    Read `size` bytes, or fewer if `timeout` runs out. A plain recv() returns
    as soon as *any* data arrives, which would split the 32-byte status.
    """
    deadline = time.monotonic() + timeout
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        conn.settimeout(remaining)
        try:
            chunk = conn.recv(size - len(data))
        except TimeoutError:
            break
        if not chunk:  # the printer closed the connection
            break
        data += chunk
    return bytes(data)


def describe_os_error(exc: OSError) -> str:
    name = errno.errorcode.get(exc.errno or 0, "")
    return f"{name} {exc.strerror or exc}".strip()


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------
Connector = Callable[[str, int, float], Connection]


class BrotherPrinter(Printer):
    def __init__(self, config: PrinterConfig, connect: Connector = rfcomm_connect):
        super().__init__(config)
        self._connect = connect

    def prepare(self, image: Image.Image, label: LabelConfig) -> Image.Image:
        # The image stays in reading orientation so the preview is readable;
        # rotation into raster lines happens in encode_job(). Same pixels.
        if image.height != label.print_height_px:
            raise PrinterError(
                f"image is {image.height} px tall but {label.name} prints {label.print_height_px}"
            )
        return image.convert("1")

    def print(self, image: Image.Image, label: LabelConfig) -> None:
        conn = self._open()
        try:
            status = self._check_ready(conn, label)
            job = encode_job(image, status)
            log.info("%s: sending %d bytes (%d lines)", self.id, len(job), image.width)
            conn.sendall(job)
            self._wait_until_printed(conn)
        except OSError as exc:
            raise PrinterUnavailable(
                f"{self.display_name}: connection lost ({describe_os_error(exc)})"
            ) from exc
        finally:
            self._close(conn)

    def status(self) -> dict:
        conn = self._open()
        try:
            status = self._query_status(conn)
            return {
                "tape_width_mm": status.tape_width_mm,
                "errors": status.error_text(),
                "ready": status.ready,
            }
        finally:
            self._close(conn)

    # --- steps ---------------------------------------------------------------
    def _open(self) -> Connection:
        started = time.monotonic()
        try:
            conn = self._connect(self.config.address, RFCOMM_CHANNEL, CONNECT_TIMEOUT_S)
        except OSError as exc:
            raise PrinterUnavailable(
                f"{self.display_name} not reachable ({describe_os_error(exc)})"
            ) from exc
        log.info("%s: connected in %d ms", self.id, (time.monotonic() - started) * 1000)
        return conn

    def _query_status(self, conn: Connection) -> Status:
        # The link can be half-awake on first use; retry a few times.
        for attempt in range(1, 4):
            conn.sendall(INVALIDATE + INITIALIZE + RASTER_MODE + STATUS_REQUEST)
            data = read_exact(conn, STATUS_BYTES, READ_TIMEOUT_S)
            if len(data) == STATUS_BYTES:
                return Status.parse(data)
            log.warning("%s: status attempt %d got %d bytes", self.id, attempt, len(data))
        raise PrinterUnavailable(f"{self.display_name} connected but didn't answer")

    def _check_ready(self, conn: Connection, label: LabelConfig) -> Status:
        status = self._query_status(conn)
        if status.errors:
            raise PrinterError(f"{self.display_name}: {status.error_text()}")
        if not status.ready:
            raise PrinterError(f"{self.display_name} is not ready (phase {status.phase_type})")
        if label.tape_width_mm and status.tape_width_mm != round(label.tape_width_mm):
            raise PrinterError(
                f"{self.display_name} has {status.tape_width_mm} mm tape loaded, "
                f"but {label.name} was chosen"
            )
        return status

    def _wait_until_printed(self, conn: Connection) -> None:
        """Read status messages until 'printing completed'. Disconnecting
        earlier can cut the job short."""
        deadline = time.monotonic() + PRINT_TIMEOUT_S
        while time.monotonic() < deadline:
            data = read_exact(conn, STATUS_BYTES, min(READ_TIMEOUT_S, deadline - time.monotonic()))
            if len(data) < STATUS_BYTES:
                continue  # nothing yet; keep waiting until the deadline
            status = Status.parse(data)
            if status.status_type == STATUS_COMPLETED:
                return
            if status.status_type == STATUS_ERROR or status.errors:
                raise PrinterError(f"{self.display_name}: {status.error_text() or 'error'}")
            if status.status_type == STATUS_POWER_OFF:
                raise PrinterUnavailable(f"{self.display_name} switched off while printing")
            # anything else (phase change, notification): keep waiting
        raise PrinterError(
            f"{self.display_name} didn't confirm the print within {PRINT_TIMEOUT_S} s"
        )

    def _close(self, conn: Connection) -> None:
        """Leave the printer clean for the next job, then hang up."""
        try:
            conn.sendall(INVALIDATE + INITIALIZE)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass
