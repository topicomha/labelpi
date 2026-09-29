"""
The printer registry: builds a backend for each configured printer and owns
one lock per printer.

Locking lives here, not in the backends. There is no queue: if a printer is
already printing, claim() fails straight away (the API turns that into 409).
The two printers have separate locks, so one can print while the other is busy.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from labelpi.config import Config, PrinterConfig
from labelpi.printers.base import Printer, PrinterError, PrinterUnavailable
from labelpi.printers.mock import MockPrinter

__all__ = [
    "Printer",
    "PrinterBusy",
    "PrinterError",
    "PrinterUnavailable",
    "Registry",
    "mock_forced",
]


class PrinterBusy(Exception):
    """The printer is already printing. API -> 409."""


def mock_forced() -> bool:
    """LABELPI_MOCK=1 turns every printer into a mock (dev and tests)."""
    return os.environ.get("LABELPI_MOCK") == "1"


def build_printer(config: PrinterConfig, force_mock: bool, out_dir: Path) -> Printer:
    if force_mock or config.type == "mock":
        return MockPrinter(config, out_dir=out_dir)
    if config.type == "brother_pt":
        from labelpi.printers.brother import BrotherPrinter

        return BrotherPrinter(config)
    # The Phomemo backend arrives in Milestone 5.
    raise NotImplementedError(
        f'printer "{config.id}": the "{config.type}" backend is not written yet - '
        "run with LABELPI_MOCK=1 for now"
    )


class Registry:
    def __init__(self, printers: list[Printer]):
        # dict keeps insertion order, so printers list in config order.
        self._printers = {p.id: p for p in printers}
        self._locks = {p.id: threading.Lock() for p in printers}

    @classmethod
    def from_config(
        cls, config: Config, force_mock: bool | None = None, out_dir: Path = Path("out")
    ) -> Registry:
        if force_mock is None:
            force_mock = mock_forced()
        return cls([build_printer(pc, force_mock, out_dir) for pc in config.printers])

    def all(self) -> list[Printer]:
        return list(self._printers.values())

    def get(self, printer_id: str) -> Printer | None:
        return self._printers.get(printer_id)

    def is_busy(self, printer_id: str) -> bool:
        return self._locks[printer_id].locked()

    @contextmanager
    def claim(self, printer_id: str) -> Iterator[Printer]:
        """
        Use as `with registry.claim("brother") as printer: ...`.

        Takes the printer's lock without waiting; raises PrinterBusy if it's
        taken. The lock is always released when the block ends, even on error
        (the `finally` - same as C#'s using/try-finally).
        """
        lock = self._locks[printer_id]
        if not lock.acquire(blocking=False):
            raise PrinterBusy(f"{printer_id} is printing")
        try:
            yield self._printers[printer_id]
        finally:
            lock.release()
