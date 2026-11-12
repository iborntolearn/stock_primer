"""OCR for scanned results PDFs (pure pip: pypdfium2 renders pages, RapidOCR/onnxruntime reads them).

OCR is used to *find* the statement page and to give the model a transcript; the values themselves are
read by LLM-15 from the page image, because OCR of dense tables tends to scramble columns. CPU OCR is slow
(seconds per page), so pages are first screened by their heading band only, scanning stops at the first
page that is clearly the wanted statement, and every OCR result is cached by (PDF hash, page, mode).
"""
from __future__ import annotations

import hashlib
import io
import logging
from typing import Any, Optional

from .adapters.base import DiskCache

log = logging.getLogger(__name__)
HEAD_BAND = 0.28            # top share of the page holding the company letterhead and statement title
HEAD_SCALE, FULL_SCALE = 1.4, 2.0
IMAGE_MAX_SIDE = 1568       # long edge for images sent to the model


def available() -> bool:
    try:
        import pypdfium2  # noqa: F401
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def lines_from(result: Optional[list]) -> str:
    """RapidOCR boxes -> text in reading order: group boxes into lines by vertical centre, then left to right."""
    if not result:
        return ""
    boxes = []
    for box, text, _score in result:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        boxes.append(((min(ys) + max(ys)) / 2, min(xs), max(ys) - min(ys), text))
    boxes.sort()
    lines: list[list[tuple]] = []
    for b in boxes:
        if lines and abs(b[0] - lines[-1][0][0]) <= max(4.0, 0.5 * b[2]):
            lines[-1].append(b)
        else:
            lines.append([b])
    return "\n".join(" ".join(t for *_, t in sorted(line, key=lambda b: b[1])) for line in lines)


class Ocr:
    def __init__(self, cache: Optional[DiskCache] = None, engine: Any = None):
        self.cache = cache
        self._engine = engine

    @property
    def engine(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        return self._engine

    def _cached(self, pdf_hash: str, page: int, mode: str, fn) -> str:
        from datetime import date

        key = {"pdf": pdf_hash, "page": page, "mode": mode}
        if self.cache is not None:
            hit = self.cache.get("ocr", mode, key, date(2000, 1, 1), "txt")
            if hit is not None:
                return hit
        text = fn()
        if self.cache is not None:
            self.cache.put("ocr", mode, key, date(2000, 1, 1), text, "txt")
        return text

    @staticmethod
    def _render(doc, page: int, scale: float):
        return doc[page].render(scale=scale).to_pil()

    def heading(self, doc, pdf_hash: str, page: int) -> str:
        def go():
            img = self._render(doc, page, HEAD_SCALE)
            w, h = img.size
            res, _ = self.engine(img.crop((0, 0, w, int(h * HEAD_BAND))), use_cls=False)
            return lines_from(res)
        return self._cached(pdf_hash, page, "head", go)

    def full(self, doc, pdf_hash: str, page: int) -> str:
        def go():
            res, _ = self.engine(self._render(doc, page, FULL_SCALE), use_cls=False)
            return lines_from(res)
        return self._cached(pdf_hash, page, "full", go)

    def png(self, doc, page: int) -> bytes:
        img = self._render(doc, page, FULL_SCALE)
        img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        buf = io.BytesIO()
        img.convert("L").save(buf, format="PNG", optimize=True)          # greyscale: smaller, same legibility
        return buf.getvalue()


def open_pdf(data: bytes):
    import pypdfium2 as pdfium

    return pdfium.PdfDocument(data), hashlib.sha256(data).hexdigest()
