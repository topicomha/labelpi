"""Phomemo D30 backend tests, against a fake D30 (no Bluetooth needed)."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from labelpi.printers import PrinterError, PrinterUnavailable, Registry
from labelpi.printers.phomemo import (
    CHUNK_BYTES,
    END_OF_JOB,
    JOB_PREFIX,
    QUERY_COVER,
    QUERY_PAPER,
    PhomemoPrinter,
    answer_byte,
    encode_job,
    to_head_orientation,
)
from labelpi.render import render_text


class FakeD30:
    """Acts like a BLE session to a D30: answers the status queries and
    acknowledges every write with 01 01, like the real one did."""

    def __init__(self, paper=0x89, cover=0x98, answers=True, fail_on_write=None, done_notice=False):
        self.paper, self.cover, self.answers = paper, cover, answers
        self.fail_on_write = fail_on_write  # raise on the Nth write
        self.done_notice = done_notice
        self.writes: list[bytes] = []
        self.inbox = bytearray()
        self.closed = False

    async def write(self, data: bytes) -> None:
        if self.fail_on_write is not None and len(self.writes) == self.fail_on_write:
            raise OSError("Connection reset by peer")
        self.writes.append(bytes(data))
        self.inbox += b"\x01\x01"
        if self.answers and data == QUERY_PAPER:
            self.inbox += bytes([0x1A, 0x06, self.paper])
        if self.answers and data == QUERY_COVER:
            self.inbox += bytes([0x1A, 0x05, self.cover])
        if self.done_notice and data.endswith(END_OF_JOB):
            self.inbox += b"\x1a\x0f\x0c"

    async def read(self, timeout: float) -> bytes:
        data = bytes(self.inbox)
        self.inbox.clear()
        return data

    async def close(self) -> None:
        self.closed = True

    @property
    def job(self) -> bytes:
        """Everything written after the status queries."""
        return b"".join(w for w in self.writes if w not in (QUERY_PAPER, QUERY_COVER))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    import labelpi.printers.phomemo as phomemo

    monkeypatch.setattr(phomemo, "PRINT_SETTLE_S", 0.05)
    monkeypatch.setattr(phomemo, "QUERY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(phomemo, "SETTLE_AFTER_CONNECT_S", 0)


@pytest.fixture
def d30_config(config):
    return config.printer("die")


def make_printer(d30_config, fake):
    async def open_session(address, timeout):
        return fake

    return PhomemoPrinter(d30_config, open_session=open_session)


def label_image(die_label):
    return render_text("Chicken soup", die_label, 203)


# --- encoding --------------------------------------------------------------------
def test_head_orientation_is_96_wide():
    rotated = to_head_orientation(Image.new("1", (400, 96), 1))
    assert rotated.size == (96, 400)


def test_rotation_direction_matches_the_hardware_test():
    """The Milestone 0 hardware test's working setting was 'rotate 270 clockwise': the label's
    top-left corner ends up bottom-left."""
    image = Image.new("1", (400, 96), 1)
    ImageDraw.Draw(image).rectangle([0, 0, 9, 9], fill=0)  # top-left block
    rotated = to_head_orientation(image)
    assert rotated.getpixel((0, 399)) == 0 and rotated.getpixel((95, 0)) != 0


def test_encode_job_bytes():
    image = Image.new("1", (96, 400), 1)
    image.putpixel((0, 0), 0)  # one black dot: first bit of the first row
    job = encode_job(image)
    header = (
        JOB_PREFIX
        + b"\x1b@"
        + b"\x1dv0\x00"
        + (12).to_bytes(2, "little")
        + (400).to_bytes(2, "little")
    )
    assert job.startswith(header)
    assert job.endswith(END_OF_JOB)
    raster = job[len(header) : -len(END_OF_JOB)]
    assert len(raster) == 12 * 400
    assert raster[0] == 0x80 and set(raster[1:]) == {0}  # 1 = black, MSB = leftmost


def test_too_wide_for_the_head():
    with pytest.raises(PrinterError, match="96"):
        encode_job(Image.new("1", (120, 10), 1))


def test_answer_byte_ignores_acks():
    assert answer_byte(b"\x01\x01\x1a\x06\x89", 0x06) == 0x89
    assert answer_byte(b"\x01\x01", 0x06) is None
    assert answer_byte(b"\x1a\x06", 0x06) is None  # value not arrived yet


# --- prepare / calibration offset -----------------------------------------------------
def test_prepare_keeps_size_and_orientation(d30_config, die_label):
    image = label_image(die_label)
    prepared = make_printer(d30_config, FakeD30()).prepare(image, die_label)
    assert prepared.size == image.size == (400, 96)


def test_offset_moves_the_print_along_the_label(d30_config, die_label):
    from dataclasses import replace

    image = Image.new("1", (400, 96), 1)
    image.putpixel((0, 50), 0)
    printer = make_printer(d30_config, FakeD30())
    later = printer.prepare(image, replace(die_label, offset_mm=2))
    assert later.size == (400, 96) and later.getpixel((16, 50)) == 0  # 2 mm = 16 px
    earlier = printer.prepare(image, replace(die_label, offset_mm=-1))
    assert earlier.getpixel((0, 50)) != 0  # white: moved off the start


# --- whole jobs ------------------------------------------------------------------
def test_print_happy_path(d30_config, die_label):
    fake = FakeD30()
    printer = make_printer(d30_config, fake)
    image = printer.prepare(label_image(die_label), die_label)
    printer.print(image, die_label)
    assert fake.writes[:2] == [QUERY_PAPER, QUERY_COVER]
    assert fake.job == encode_job(to_head_orientation(image))
    assert all(len(w) <= CHUNK_BYTES for w in fake.writes)
    assert fake.closed


def test_done_notice_ends_the_wait_early(d30_config, die_label, monkeypatch):
    import time

    import labelpi.printers.phomemo as phomemo

    monkeypatch.setattr(phomemo, "PRINT_SETTLE_S", 5)
    started = time.monotonic()
    make_printer(d30_config, FakeD30(done_notice=True)).print(label_image(die_label), die_label)
    assert time.monotonic() - started < 2


def test_no_labels_loaded(d30_config, die_label):
    fake = FakeD30(paper=0x88)
    with pytest.raises(PrinterError, match="no labels loaded"):
        make_printer(d30_config, fake).print(label_image(die_label), die_label)
    assert fake.job == b"" and fake.closed


def test_cover_open(d30_config, die_label):
    with pytest.raises(PrinterError, match="cover is open"):
        make_printer(d30_config, FakeD30(cover=0x99)).print(label_image(die_label), die_label)


def test_silent_printer_is_not_sent_the_job(d30_config, die_label):
    """No answer at all = probably stuck mid-image: the job would print garbled."""
    fake = FakeD30(answers=False)
    with pytest.raises(PrinterError, match="switch it off and on"):
        make_printer(d30_config, fake).print(label_image(die_label), die_label)
    assert not fake.job.endswith(END_OF_JOB)
    assert fake.closed


def test_not_reachable(d30_config, die_label):
    async def refuse(address, timeout):
        raise TimeoutError()

    printer = PhomemoPrinter(d30_config, open_session=refuse)
    with pytest.raises(PrinterUnavailable, match="not reachable"):
        printer.print(label_image(die_label), die_label)


def test_wrong_device(d30_config, die_label):
    async def not_a_d30(address, timeout):
        raise PrinterError("connected, but this isn't a Phomemo D30 (no FF00/FF02)")

    with pytest.raises(PrinterError, match="isn't a Phomemo"):
        PhomemoPrinter(d30_config, open_session=not_a_d30).print(label_image(die_label), die_label)


def test_connection_drops_mid_job(d30_config, die_label):
    fake = FakeD30(fail_on_write=5)
    with pytest.raises(PrinterUnavailable, match=r"connection lost after \d+ of \d+ bytes"):
        make_printer(d30_config, fake).print(label_image(die_label), die_label)
    assert fake.closed


def test_drop_before_the_image_is_retried_once(d30_config, die_label):
    """Nothing of the image was sent, so a fresh connection can try again."""
    first = FakeD30(fail_on_write=2)  # fails right after the two status queries
    second = FakeD30()
    sessions = [first, second]

    async def open_session(address, timeout):
        return sessions.pop(0)

    printer = PhomemoPrinter(d30_config, open_session=open_session)
    printer.print(label_image(die_label), die_label)
    assert first.job == b"" and first.closed
    assert second.job.endswith(END_OF_JOB) and second.closed


def test_drop_before_the_image_twice_gives_up(d30_config, die_label):
    fake = FakeD30(fail_on_write=2)  # the same fake fails both attempts
    with pytest.raises(PrinterUnavailable, match="connection lost") as caught:
        make_printer(d30_config, fake).print(label_image(die_label), die_label)
    assert "off and on" not in str(caught.value)  # nothing half-sent: no power cycle


def test_drop_mid_image_is_not_retried(d30_config, die_label):
    """Retrying would print into the half-sent image."""
    opened = []

    async def open_session(address, timeout):
        opened.append(1)
        return FakeD30(fail_on_write=5)

    with pytest.raises(PrinterUnavailable, match="off and on"):
        PhomemoPrinter(d30_config, open_session=open_session).print(
            label_image(die_label), die_label
        )
    assert len(opened) == 1


def test_status(d30_config):
    info = make_printer(d30_config, FakeD30()).status()
    assert info == {"labels_loaded": True, "cover_open": False, "battery_percent": None}


def test_registry_builds_real_backends(config, tmp_path):
    registry = Registry.from_config(config, force_mock=False, out_dir=tmp_path)
    assert type(registry.get("die")).__name__ == "PhomemoPrinter"
    assert type(registry.get("tape")).__name__ == "BrotherPrinter"
