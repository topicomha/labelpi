"""
The interface every printer backend implements.

Backends only ever receive a finished image from render.py - they know
nothing about text. They connect for each job and disconnect afterwards:
the printers go to sleep, and stale connections are the main source of
flakiness.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from PIL import Image, ImageDraw

from labelpi.config import LabelConfig, PrinterConfig


class PrinterUnavailable(Exception):
    """Printer off, out of range or not answering. API -> 503."""


class PrinterError(Exception):
    """Printer reported an error, or something unexpected broke. API -> 500."""


class Printer(ABC):
    """
    Base class (like an abstract class in C#) for one configured printer.

    Chaining (continuous tape only): normally each label is fed out past the
    cutter when it's done, which leaves ~24 mm of blank tape at the start of
    the next one. With chain=True labels print back to back instead, with a
    dashed cut line at both ends of each, and feed() pushes the finished strip
    out to the cutter at the end.
    """

    #: True if this printer can print labels back to back (see chain above).
    can_chain: bool = False

    def __init__(self, config: PrinterConfig):
        self.config = config

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def display_name(self) -> str:
        return self.config.display_name

    @property
    def labels(self) -> tuple[LabelConfig, ...]:
        return self.config.labels

    @abstractmethod
    def prepare(self, image: Image.Image, label: LabelConfig, chain: bool = False) -> Image.Image:
        """Rotate/pad/convert a rendered label into exactly what this printer takes
        (plus cut lines if chaining). Preview shows the result of this, so it must
        not have side effects."""

    @abstractmethod
    def print(self, image: Image.Image, label: LabelConfig, chain: bool = False) -> None:
        """Connect, send the prepared image, wait until done, disconnect.
        chain=True: don't feed the label out afterwards (only if can_chain).
        Raise PrinterUnavailable or PrinterError on failure."""

    def feed(self) -> None:
        """Feed the tape out to the cutter (after chained labels)."""
        raise PrinterError(f"{self.display_name} has nothing to feed")

    def status(self) -> dict:
        """Best-effort status (battery, tape...). Empty if the backend has none."""
        return {}


CUT_DASH_PX = 4  # dash and gap length of the cut lines


def add_cut_lines(image: Image.Image) -> Image.Image:
    """
    A copy of a label image with a dashed line down both ends: where to cut
    when labels were printed back to back. Labels have margins at their ends,
    so the lines don't touch the content.
    """
    marked = image.convert("1").copy()
    draw = ImageDraw.Draw(marked)
    for x in (0, 1, marked.width - 2, marked.width - 1):  # 2 px wide at each end
        for y in range(0, marked.height, 2 * CUT_DASH_PX):
            draw.line([(x, y), (x, min(y + CUT_DASH_PX - 1, marked.height - 1))], fill=0)
    return marked
