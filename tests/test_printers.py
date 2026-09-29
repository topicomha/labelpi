from __future__ import annotations

import threading

import pytest
from PIL import Image

from labelpi.printers import PrinterBusy, Registry, mock_forced
from labelpi.printers.mock import MockPrinter
from labelpi.render import render_text


@pytest.fixture
def registry(config, tmp_path):
    return Registry.from_config(config, out_dir=tmp_path / "out")


def test_labelpi_mock_env_forces_mock(registry):
    assert mock_forced()
    assert all(isinstance(p, MockPrinter) for p in registry.all())
    assert [p.id for p in registry.all()] == ["tape", "die"]


def test_unknown_printer(registry):
    assert registry.get("nope") is None


def test_claim_marks_printer_busy_and_releases(registry):
    assert not registry.is_busy("tape")
    with registry.claim("tape") as printer:
        assert printer.id == "tape"
        assert registry.is_busy("tape")
    assert not registry.is_busy("tape")


def test_second_claim_fails_immediately(registry):
    with registry.claim("tape"):
        with pytest.raises(PrinterBusy, match="tape is printing"):
            with registry.claim("tape"):
                pass


def test_printers_are_independent(registry):
    with registry.claim("tape"):
        with registry.claim("die") as other:
            assert other.id == "die"


def test_lock_released_after_error(registry):
    with pytest.raises(RuntimeError):
        with registry.claim("die"):
            raise RuntimeError("printer fell over")
    assert not registry.is_busy("die")


def test_busy_across_threads(registry):
    """The real case: a request thread is printing when another one arrives."""
    holding = threading.Event()
    release = threading.Event()

    def print_slowly():
        with registry.claim("tape"):
            holding.set()
            release.wait(5)

    worker = threading.Thread(target=print_slowly)
    worker.start()
    try:
        assert holding.wait(5)
        with pytest.raises(PrinterBusy):
            with registry.claim("tape"):
                pass
    finally:
        release.set()
        worker.join()
    assert not registry.is_busy("tape")


def test_mock_printer_writes_png(registry, tmp_path):
    printer = registry.get("die")
    label = printer.config.label("12x40")
    image = printer.prepare(render_text("Hello", label, printer.config.dpi), label)
    printer.print(image, label)
    [path] = printer.printed
    assert path.parent == tmp_path / "out"
    assert path.name.endswith("-die-12x40.png")
    assert Image.open(path).size == (320, 96)
