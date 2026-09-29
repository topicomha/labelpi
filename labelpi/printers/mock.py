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
from labelpi.printers.base import Printer, add_cut_lines

log = logging.getLogger(__name__)


class MockPrinter(Printer):
    def __init__(self, config: PrinterConfig, out_dir: Path = Path("out")):
        super().__init__(config)
        self.out_dir = out_dir
        self.printed: list[Path] = []  # handy for tests
        self.chained: list[bool] = []  # chain flag of each print, for tests
        self.feeds = 0
        # Behave like the printer it stands in for: tape printers can chain.
        self.can_chain = config.type == "brother_pt"

    def prepare(self, image: Image.Image, label: LabelConfig, chain: bool = False) -> Image.Image:
        # No rotation: the mock "prints" the label the way you'd read it.
        if chain and self.can_chain:
            return add_cut_lines(image)
        return image.convert("1")

    def feed(self) -> None:
        if not self.can_chain:
            super().feed()
        self.feeds += 1
        log.info("mock printer %s: fed the tape out", self.id)

    def print(self, image: Image.Image, label: LabelConfig, chain: bool = False) -> None:
        self.chained.append(chain)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = self.out_dir / f"{stamp}-{self.id}-{label.id}.png"
        image.save(path)
        self.printed.append(path)
        log.info("mock printer %s: wrote %s", self.id, path)

    def status(self) -> dict:
        return {"mock": True, "out_dir": str(self.out_dir)}
