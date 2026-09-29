"""
Phomemo D30 backend: Bluetooth Low Energy (GATT) + ESC/POS raster.

Our own implementation. The byte sequences are facts documented by
polskafan/phomemo_d30 (MIT) and odensc/phomemo-d30-web-bluetooth
(Apache-2.0), and were checked against a real D30 in the Milestone 0 spike
(docs/SPEC.md section 10): over BLE it printed; over Classic RFCOMM it
connected but a job didn't print, so we use BLE.

A print job:
    connect over BLE by address (no pairing needed)
    ask whether labels are loaded and the cover is closed; refuse if not
    send: header, the image as 96-px rows (1 bit per dot), end-of-job,
          in 128-byte writes that the printer acknowledges one by one
    give it a few seconds to print, then disconnect

bleak (the BLE library) is asyncio-only; the web app is plain threads. Each
job runs in its own event loop via asyncio.run() on the request's thread -
like calling an async method with .GetAwaiter().GetResult() in C#.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from PIL import Image

from labelpi.config import LabelConfig, PrinterConfig
from labelpi.printers.base import Printer, PrinterError, PrinterUnavailable

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 20  # includes BlueZ finding the printer if it isn't cached
QUERY_TIMEOUT_S = 1.5
POWER_CYCLE_HINT = "switch it off and on, then print again"
SETTLE_AFTER_CONNECT_S = 0.5  # writing at once after connecting failed ("GATT Unlikely Error")
PRINT_SETTLE_S = 4.0  # the D30 doesn't say when it's done; the spike waited 4 s
CHUNK_BYTES = 128  # "works best with 128 bytes" (odensc); each write is acknowledged
HEAD_PX = 96  # print head width: 12 bytes per row
PX_PER_MM = 8  # 203 dpi

SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

# Status queries (1F 11 xx) and their answers (1A xx yy).
QUERY_PAPER = b"\x1f\x11\x11"  # -> 1A 06 89 labels loaded / 88 none
QUERY_COVER = b"\x1f\x11\x12"  # -> 1A 05 98 closed / 99 open
QUERY_BATTERY = b"\x1f\x11\x08"  # -> 1A 04 <percent> (best effort)
ANSWER_PAPER, ANSWER_COVER, ANSWER_BATTERY = 0x06, 0x05, 0x04
PAPER_MISSING, COVER_OPEN = 0x88, 0x99
DONE_NOTICE = b"\x1a\x0f"  # seen after a job over Classic; if it comes, we stop waiting early

JOB_PREFIX = b"\x1f\x11\x24\x00"  # sent by the phone app before every image
RESET = b"\x1b@"  # ESC @
END_OF_JOB = b"\x1bd\x00"  # ESC d 0


# ---------------------------------------------------------------------------
# Encoding (pure functions - easy to test without a printer)
# ---------------------------------------------------------------------------
def to_head_orientation(image: Image.Image) -> Image.Image:
    """
    The label is drawn as you read it (x along the label). The D30 prints
    rows across the label, so turn it 270 degrees clockwise (= 90 anticlockwise,
    which is Pillow's direction), as the spike did.
    """
    return image.convert("1").rotate(90, expand=True)


def encode_job(image: Image.Image) -> bytes:
    """Header + rows + end for an image already in head orientation."""
    if image.width > HEAD_PX:
        raise PrinterError(f"label image is {image.width} px wide; the D30 head has {HEAD_PX}")
    width_bytes = (image.width + 7) // 8
    rows = image.height
    header = (
        JOB_PREFIX
        + RESET
        + b"\x1dv0\x00"  # GS v 0: raster image, normal size
        + width_bytes.to_bytes(2, "little")
        + rows.to_bytes(2, "little")
    )
    # Pillow's mode "1" stores white as 1; the printer wants 1 = black dot.
    raster = bytes(b ^ 0xFF for b in image.convert("1").tobytes())
    return header + raster + END_OF_JOB


def answer_byte(data: bytes, code: int) -> int | None:
    """The value byte after 1A <code> in the printer's replies, if present."""
    marker = bytes([0x1A, code])
    at = data.find(marker)
    if at < 0 or at + 2 >= len(data):
        return None
    return data[at + 2]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
class _DroppedBeforeImage(PrinterUnavailable):
    """The link dropped before any image data was sent: safe to try again."""


class Session(Protocol):
    """One BLE connection to the printer - lets tests pass in a fake D30."""

    async def write(self, data: bytes) -> None: ...
    async def read(self, timeout: float) -> bytes: ...
    async def close(self) -> None: ...


class BleakSession:
    """The real thing: bleak over BlueZ."""

    def __init__(self, client, characteristic):
        self._client = client
        self._char = characteristic
        self._received = bytearray()
        self._arrived = asyncio.Event()

    @classmethod
    async def open(cls, address: str, timeout: float) -> BleakSession:
        from bleak import BleakClient  # imported here so tests and mock runs don't need it

        client = BleakClient(address, timeout=timeout)
        await client.connect()
        try:
            service = client.services.get_service(SERVICE_UUID)
            char = service.get_characteristic(WRITE_UUID) if service else None
            if char is None:
                raise PrinterError("connected, but this isn't a Phomemo D30 (no FF00/FF02)")
            session = cls(client, char)
            for c in service.characteristics:
                if "notify" in c.properties:
                    await client.start_notify(c, session._on_notify)
            return session
        except BaseException:
            await client.disconnect()
            raise

    def _on_notify(self, _characteristic, data: bytearray) -> None:
        self._received += data
        self._arrived.set()

    async def write(self, data: bytes) -> None:
        await self._client.write_gatt_char(self._char, data, response=True)

    async def read(self, timeout: float) -> bytes:
        """Everything received so far; waits up to `timeout` if nothing yet."""
        if not self._received:
            try:
                await asyncio.wait_for(self._arrived.wait(), timeout)
            except TimeoutError:
                pass
        data = bytes(self._received)
        self._received.clear()
        self._arrived.clear()
        return data

    async def close(self) -> None:
        await self._client.disconnect()


Opener = Callable[[str, float], Awaitable[Session]]


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------
class PhomemoPrinter(Printer):
    def __init__(self, config: PrinterConfig, open_session: Opener = BleakSession.open):
        super().__init__(config)
        self._open_session = open_session

    def prepare(self, image: Image.Image, label: LabelConfig) -> Image.Image:
        """
        Apply the label's calibration offset along its length, keeping the
        label size: positive offset_mm moves the print later (blank space
        added at the start, the end trimmed), negative moves it earlier.
        Stays in reading orientation so the preview is readable.
        """
        image = image.convert("1")
        shift = round(label.offset_mm * PX_PER_MM)
        if shift == 0:
            return image
        shifted = Image.new("1", image.size, 1)
        shifted.paste(image, (shift, 0))
        return shifted

    def print(self, image: Image.Image, label: LabelConfig) -> None:
        job = encode_job(to_head_orientation(image))
        try:
            asyncio.run(self._print(job))
        except _DroppedBeforeImage as first:
            # The first job after the D30 has been idle sometimes loses the
            # link before any image data went out. Nothing half-sent is left
            # in the printer, so one fresh attempt is safe.
            log.warning("%s: %s - trying once more", self.id, first)
            try:
                asyncio.run(self._print(job))
            except _DroppedBeforeImage as second:
                raise PrinterUnavailable(str(second)) from second

    def status(self) -> dict:
        return asyncio.run(self._status())

    # --- async steps -------------------------------------------------------------
    async def _print(self, job: bytes) -> None:
        session = await self._open()
        sent = 0
        try:
            await self._check_ready(session)
            started = time.monotonic()
            for start in range(0, len(job), CHUNK_BYTES):
                await session.write(job[start : start + CHUNK_BYTES])
                sent = start + CHUNK_BYTES
            log.info(
                "%s: sent %d bytes in %d ms", self.id, len(job), (time.monotonic() - started) * 1000
            )
            await self._let_it_print(session)
        except (PrinterError, PrinterUnavailable):
            raise
        except Exception as exc:  # BleakError, OSError, EOFError... a dropped link
            if 0 < sent < len(job):
                # The D30 now waits for the rest of this image and would take
                # the next job's bytes as that rest: a garbled, shifted label.
                # Nothing we can send fixes it; a power cycle does.
                raise PrinterUnavailable(
                    f"{self.display_name}: connection lost after {sent} of {len(job)} bytes "
                    f"({exc}) - it may be stuck on the half-sent label: {POWER_CYCLE_HINT}"
                ) from exc
            raise _DroppedBeforeImage(f"{self.display_name}: connection lost ({exc})") from exc
        finally:
            await self._close(session)

    async def _status(self) -> dict:
        session = await self._open()
        try:
            paper = await self._ask(session, QUERY_PAPER, ANSWER_PAPER)
            cover = await self._ask(session, QUERY_COVER, ANSWER_COVER)
            battery = await self._ask(session, QUERY_BATTERY, ANSWER_BATTERY)
            return {
                "labels_loaded": None if paper is None else paper != PAPER_MISSING,
                "cover_open": None if cover is None else cover == COVER_OPEN,
                "battery_percent": battery,
            }
        finally:
            await self._close(session)

    async def _open(self) -> Session:
        started = time.monotonic()
        try:
            session = await self._open_session(self.config.address, CONNECT_TIMEOUT_S)
        except PrinterError:
            raise
        except Exception as exc:  # BleakDeviceNotFoundError, BleakError, TimeoutError, OSError
            raise PrinterUnavailable(
                f"{self.display_name} not reachable ({exc or type(exc).__name__})"
            ) from exc
        log.info("%s: connected in %d ms", self.id, (time.monotonic() - started) * 1000)
        await asyncio.sleep(SETTLE_AFTER_CONNECT_S)
        return session

    async def _ask(self, session: Session, query: bytes, code: int) -> int | None:
        await session.write(query)
        deadline = time.monotonic() + QUERY_TIMEOUT_S
        received = b""
        while time.monotonic() < deadline:
            received += await session.read(deadline - time.monotonic())
            value = answer_byte(received, code)
            if value is not None:
                return value
        return None

    async def _check_ready(self, session: Session) -> None:
        paper = await self._ask(session, QUERY_PAPER, ANSWER_PAPER)
        cover = await self._ask(session, QUERY_COVER, ANSWER_COVER)
        if cover == COVER_OPEN:
            raise PrinterError(f"{self.display_name}: the cover is open")
        if paper == PAPER_MISSING:
            raise PrinterError(f"{self.display_name}: no labels loaded")
        if paper is None and cover is None:
            # A D30 that still waits for the rest of an interrupted image takes
            # our queries as picture data, so it can't answer. Printing now
            # would come out shifted and garbled (seen 2026-09-28), so don't.
            raise PrinterError(
                f"{self.display_name} didn't answer the status check - it may be stuck on "
                f"an interrupted label: {POWER_CYCLE_HINT}"
            )
        if paper is None or cover is None:
            # One answer came, so it's listening; just note the missing one.
            log.warning(
                "%s: didn't answer the status check (paper=%s cover=%s)", self.id, paper, cover
            )

    async def _let_it_print(self, session: Session) -> None:
        """
        The D30 sends no 'finished' message over BLE, and hanging up too early
        can cut the label short. Wait PRINT_SETTLE_S, or less if a
        done-notice happens to arrive.
        """
        deadline = time.monotonic() + PRINT_SETTLE_S
        while (remaining := deadline - time.monotonic()) > 0:
            if DONE_NOTICE in await session.read(remaining):
                return

    async def _close(self, session: Session) -> None:
        try:
            await session.close()
        except Exception as exc:  # already disconnected, etc.
            log.debug("%s: close: %s", self.id, exc)
