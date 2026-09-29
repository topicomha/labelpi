"""Brother backend tests, against a fake printer that speaks the protocol."""

from __future__ import annotations

import errno
import struct

import pytest
from PIL import Image, ImageDraw

from labelpi.printers import PrinterError, PrinterUnavailable
from labelpi.printers.brother import (
    INITIALIZE,
    PRINT_AND_FEED,
    RASTER_PX,
    STATUS_REQUEST,
    BrotherPrinter,
    Status,
    encode_job,
    packbits,
    raster_lines,
)
from labelpi.render import render_text


# --- helpers -------------------------------------------------------------------
def unpackbits(data: bytes) -> bytes:
    """Reference PackBits decoder, to check our encoder round-trips."""
    out, i = bytearray(), 0
    while i < len(data):
        n = data[i]
        i += 1
        if n < 128:
            out += data[i : i + n + 1]
            i += n + 1
        elif n > 128:
            out += bytes([data[i]]) * (257 - n)
            i += 1
    return bytes(out)


def status_bytes(
    *, errors=0, tape_width=12, tape_type=0x01, status_type=0x00, phase_type=0, phase=0
) -> bytes:
    data = bytearray(32)
    data[0:4] = b"\x80\x20B0"
    data[4] = 0x72  # PT-P300BT
    data[8:10] = struct.pack(">H", errors)
    data[10] = tape_width
    data[11] = tape_type
    data[18] = status_type
    data[19] = phase_type
    data[20:22] = struct.pack(">H", phase)
    return bytes(data)


class FakePrinter:
    """
    Acts like the RFCOMM socket to a PT-P300BT. Answers a status request with
    `status`, and a finished job with `after_print` (default: phase change,
    then printing completed). Replies arrive in small pieces on purpose,
    like real Bluetooth does.
    """

    def __init__(self, status=None, after_print=None):
        self.status = status if status is not None else status_bytes()
        self.after_print = (
            after_print
            if after_print is not None
            else [status_bytes(status_type=0x06, phase_type=1), status_bytes(status_type=0x01)]
        )
        self.sent = bytearray()
        self.jobs: list[bytes] = []
        self._inbox = bytearray()
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent += data
        if data.endswith(STATUS_REQUEST):
            self._inbox += self.status
        elif data.endswith(PRINT_AND_FEED):
            self.jobs.append(bytes(data))
            for message in self.after_print:
                self._inbox += message

    def recv(self, size: int) -> bytes:
        if not self._inbox:
            raise TimeoutError
        chunk = bytes(self._inbox[: min(size, 7)])  # deliberately fragmented
        del self._inbox[: len(chunk)]
        return chunk

    def settimeout(self, timeout) -> None:
        pass

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def brother_config(config):
    return config.printer("tape")


@pytest.fixture
def fast_timeouts(monkeypatch):
    """Make 'printer never answers' tests finish quickly."""
    import labelpi.printers.brother as brother

    monkeypatch.setattr(brother, "READ_TIMEOUT_S", 0.05)
    monkeypatch.setattr(brother, "PRINT_TIMEOUT_S", 0.2)


def make_printer(brother_config, fake):
    return BrotherPrinter(brother_config, connect=lambda address, channel, timeout: fake)


def label_image(tape_label):
    return render_text("Server rack 2", tape_label, 180)


# --- PackBits ------------------------------------------------------------------
@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00" * 16,
        b"\xff" * 16,
        bytes(range(16)),
        b"\xaa\xaa\xaa\x80\x00\x2a\xaa\xaa\xaa\xaa\x80\x00\x2a\x22" + b"\xaa" * 10,
        bytes(range(256)) * 2,
        b"\x01\x01" + bytes(range(200)) + b"\x02" * 300,
    ],
)
def test_packbits_round_trips(data):
    assert unpackbits(packbits(data)) == data


def test_packbits_compresses_runs():
    assert packbits(b"\x00" * 16) == b"\xf1\x00"  # 257 - 16 = 0xf1


def test_packbits_known_example():
    """Apple's reference example: our output must decode to the same input."""
    source = bytes.fromhex("AAAAAA80002AAAAAAAAA80002A22AAAAAAAAAAAAAAAAAAAA")
    assert unpackbits(packbits(source)) == source
    assert len(packbits(source)) <= len(bytes.fromhex("FEAA0280002AFDAA0380002A22F7AA"))


# --- raster --------------------------------------------------------------------
def test_raster_lines_centre_the_band():
    """A fully black 64-px band lands on rows 32-95 of the 128 (as measured)."""
    image = Image.new("1", (10, 64), 0)
    lines = raster_lines(image)
    assert len(lines) == 10 and all(len(line) == 16 for line in lines)
    bits = "".join(f"{byte:08b}" for byte in lines[0])
    assert bits == "0" * 32 + "1" * 64 + "0" * 32


def test_first_raster_line_is_left_edge_of_label():
    image = Image.new("1", (20, 64), 1)  # white
    ImageDraw.Draw(image).line([(0, 0), (0, 63)], fill=0)  # black left edge only
    lines = raster_lines(image)
    assert any(lines[0]) and not any(lines[-1])


def test_label_taller_than_head_is_rejected():
    with pytest.raises(PrinterError, match="128"):
        raster_lines(Image.new("1", (10, RASTER_PX + 1), 1))


def test_encode_job_structure(tape_label):
    image = label_image(tape_label)
    job = encode_job(image, Status.parse(status_bytes()))
    assert job.startswith(b"\x00" * 64 + INITIALIZE + b"\x1bia\x01")
    assert job.endswith(PRINT_AND_FEED)
    info = job.index(b"\x1biz") + 3
    flags, media, width, length, lines, _, _ = struct.unpack("<4BI2B", job[info : info + 10])
    assert (media, width, length, lines) == (0x01, 12, 0, image.width)
    assert b"M\x02" in job  # PackBits compression on
    assert b"Z" in job  # blank lines at the label's ends are sent as 'Z'


def test_status_parse_and_errors():
    status = Status.parse(status_bytes(errors=(1 << 4) | (1 << 8)))
    assert status.tape_width_mm == 12 and status.model == 0x72
    assert status.error_text() == "cover is open, no tape loaded"
    assert not status.ready


def test_status_rejects_garbage():
    with pytest.raises(PrinterError):
        Status.parse(b"\x00" * 32)


# --- whole print jobs ------------------------------------------------------------
def test_print_happy_path(brother_config, tape_label):
    fake = FakePrinter()
    printer = make_printer(brother_config, fake)
    image = printer.prepare(label_image(tape_label), tape_label)
    printer.print(image, tape_label)
    assert len(fake.jobs) == 1
    assert fake.closed
    assert fake.sent.endswith(b"\x00" * 64 + INITIALIZE)  # left the printer clean


def test_prepare_keeps_reading_orientation(brother_config, tape_label):
    image = label_image(tape_label)
    prepared = make_printer(brother_config, FakePrinter()).prepare(image, tape_label)
    assert prepared.size == image.size and prepared.mode == "1"


def test_printer_error_is_reported(brother_config, tape_label):
    fake = FakePrinter(status=status_bytes(errors=1 << 4))
    with pytest.raises(PrinterError, match="cover is open"):
        make_printer(brother_config, fake).print(label_image(tape_label), tape_label)
    assert fake.jobs == [] and fake.closed


def test_wrong_tape_loaded(brother_config, tape_label):
    fake = FakePrinter(status=status_bytes(tape_width=9))
    with pytest.raises(PrinterError, match="9 mm tape loaded"):
        make_printer(brother_config, fake).print(label_image(tape_label), tape_label)
    assert fake.jobs == []


def test_error_during_printing(brother_config, tape_label):
    fake = FakePrinter(after_print=[status_bytes(status_type=0x02, errors=1 << 9)])
    with pytest.raises(PrinterError, match="end of tape"):
        make_printer(brother_config, fake).print(label_image(tape_label), tape_label)


def test_switched_off_while_printing(brother_config, tape_label):
    fake = FakePrinter(after_print=[status_bytes(status_type=0x04)])
    with pytest.raises(PrinterUnavailable, match="switched off"):
        make_printer(brother_config, fake).print(label_image(tape_label), tape_label)


def test_no_completion_message(brother_config, tape_label, fast_timeouts):
    fake = FakePrinter(after_print=[])
    with pytest.raises(PrinterError, match="didn't confirm"):
        make_printer(brother_config, fake).print(label_image(tape_label), tape_label)


def test_printer_never_answers_status(brother_config, tape_label, fast_timeouts):
    fake = FakePrinter(status=b"")
    with pytest.raises(PrinterUnavailable, match="didn't answer"):
        make_printer(brother_config, fake).print(label_image(tape_label), tape_label)


def test_printer_off_or_out_of_range(brother_config, tape_label):
    def refuse(address, channel, timeout):
        raise OSError(errno.EHOSTDOWN, "Host is down")

    printer = BrotherPrinter(brother_config, connect=refuse)
    with pytest.raises(PrinterUnavailable, match="not reachable"):
        printer.print(label_image(tape_label), tape_label)


def test_busy_link_is_retried(brother_config, tape_label, monkeypatch):
    """Right after a job the old link may still be closing: EBUSY, then fine."""
    import labelpi.printers.brother as brother

    monkeypatch.setattr(brother, "BUSY_RETRY_EVERY_S", 0)
    fake, attempts = FakePrinter(), []

    def busy_twice(address, channel, timeout):
        attempts.append(1)
        if len(attempts) <= 2:
            raise OSError(errno.EBUSY, "Device or resource busy")
        return fake

    printer = BrotherPrinter(brother_config, connect=busy_twice)
    printer.print(label_image(tape_label), tape_label)
    assert len(attempts) == 3 and len(fake.jobs) == 1


def test_busy_for_too_long_gives_up(brother_config, tape_label, monkeypatch):
    import labelpi.printers.brother as brother

    monkeypatch.setattr(brother, "BUSY_RETRY_S", 0.05)
    monkeypatch.setattr(brother, "BUSY_RETRY_EVERY_S", 0.01)

    def always_busy(address, channel, timeout):
        raise OSError(errno.EBUSY, "Device or resource busy")

    with pytest.raises(PrinterUnavailable, match="EBUSY"):
        BrotherPrinter(brother_config, connect=always_busy).print(
            label_image(tape_label), tape_label
        )


def test_connection_drops_mid_job(brother_config, tape_label):
    class Dropping(FakePrinter):
        def sendall(self, data):
            if data.endswith(PRINT_AND_FEED):
                raise ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
            super().sendall(data)

    fake = Dropping()
    with pytest.raises(PrinterUnavailable, match="connection lost"):
        make_printer(brother_config, fake).print(label_image(tape_label), tape_label)
    assert fake.closed


def test_status_method(brother_config):
    info = make_printer(brother_config, FakePrinter()).status()
    assert info == {"tape_width_mm": 12, "errors": "", "ready": True}


# --- chain printing and feed ---------------------------------------------------------
def advanced_mode(job: bytes) -> int:
    """The ESC i K byte: 0x08 = feed out after the label, 0x00 = chain."""
    return job[job.index(b"\x1biK") + 3]


def test_chain_turns_off_the_feed_out(tape_label):
    image = label_image(tape_label)
    status = Status.parse(status_bytes())
    assert advanced_mode(encode_job(image, status)) == 0x08
    assert advanced_mode(encode_job(image, status, chain=True)) == 0x00


def test_chained_print_and_feed(brother_config, tape_label):
    fake = FakePrinter()
    printer = make_printer(brother_config, fake)
    assert printer.can_chain
    printer.print(
        printer.prepare(label_image(tape_label), tape_label, chain=True), tape_label, True
    )
    printer.feed()
    label_job, feed_job = fake.jobs
    assert advanced_mode(label_job) == 0x00
    # The feed is a one-line blank job that does feed out.
    assert advanced_mode(feed_job) == 0x08
    info = feed_job.index(b"\x1biz") + 3
    assert struct.unpack("<4BI2B", feed_job[info : info + 10])[4] == 1
    assert feed_job.count(b"G") == 0  # nothing black to print


def test_chained_prepare_adds_cut_lines(brother_config, tape_label):
    printer = make_printer(brother_config, FakePrinter())
    image = label_image(tape_label)
    plain = printer.prepare(image, tape_label)
    marked = printer.prepare(image, tape_label, chain=True)
    assert marked.size == plain.size
    assert plain.getpixel((0, 0)) != 0 and marked.getpixel((0, 0)) == 0
    assert marked.getpixel((marked.width - 1, 0)) == 0
