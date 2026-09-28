"""
A pretend printer that saves each label as a PNG in ./out/ instead of printing.

Used for local development and all tests (LABELPI_MOCK=1 forces it for every
printer), and for printers configured with type = "mock".
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PIL import Image

from labelpi.config import LabelConfig, PrinterConfig
from labelpi.printers.base import Printer

log = logging.getLogger(__name__)


class MockPrinter(Printer):
    def __init__(self, config: PrinterConfig, out_dir: Path = Path("out")):
        super().__init__(config)
        self.out_dir = out_dir
        self.printed: list[Path] = []  # handy for tests

    def prepare(self, image: Image.Image, label: LabelConfig) -> Image.Image:
        # No rotation: the mock "prints" the label the way you'd read it.
        return image.convert("1")

    def print(self, image: Image.Image, label: LabelConfig) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = self.out_dir / f"{stamp}-{self.id}-{label.id}.png"
        image.save(path)
        self.printed.append(path)
        log.info("mock printer %s: wrote %s", self.id, path)

    def status(self) -> dict:
        return {"mock": True, "out_dir": str(self.out_dir)}
