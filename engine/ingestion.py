"""Document ingestion: file validation, text extraction, OCR fallback.

Supported input types: PDF (.pdf) and plain text (.txt / text/plain).
Size cap: 25 MB.
OCR fallback: any PDF page whose extracted text is below OCR_MIN_CHARS
characters is re-processed with Tesseract if it is available; otherwise a
warning is recorded and the short text is kept as-is.
Reading order: PDF pages are read block by block (engine/layout.py), so columns,
margin notes and boxes don't get spliced into each other's sentences. With
order_with_model, a model decides the order on pages with blocks side by side.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)

MAX_FILE_BYTES: int = 25 * 1024 * 1024  # 25 MB
OCR_MIN_CHARS: int = 100               # chars below which a page is considered image-only

SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".md"}
SUPPORTED_MIME_PREFIXES = ("application/pdf", "text/")


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class ExtractionResult:
    filename: str
    content_type: str
    size_bytes: int
    page_count: int
    char_count: int
    ocr_pages: List[int]      # 1-indexed page numbers that required OCR
    ocr_skipped_pages: List[int]  # pages that needed OCR but Tesseract unavailable
    text: str
    layout: dict = field(default_factory=dict)   # pages per reading-order method


# ─── Tesseract availability check ─────────────────────────────────────────────

def _tesseract_available() -> bool:
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


_TESSERACT_OK: bool | None = None  # cached after first check


def _ocr_page(pil_image) -> str:
    import pytesseract
    return pytesseract.image_to_string(pil_image)


# ─── Extraction helpers ────────────────────────────────────────────────────────

def _page_text(page, orderer, layout: dict) -> str:
    """One page's text in reading order. Rotated text (watermarks, spine titles) is dropped."""
    from .layout import order_page
    try:
        # x_tolerance 1.5 (default 3): tightly set PDFs otherwise run words together
        # ("presentaresiduallearning"); it doesn't split words in normally spaced text.
        words = [w for w in page.extract_words(x_tolerance=1.5, extra_attrs=["size", "upright"])
                 if w.get("upright", True)]
        result = order_page(words, page.width, page.height, orderer)
    except Exception as exc:   # an odd page shouldn't sink the document
        logger.warning("layout extraction failed, using plain text: %s", exc)
        layout["plain"] = layout.get("plain", 0) + 1
        return page.extract_text() or ""
    layout[result.method] = layout.get(result.method, 0) + 1
    return result.text


def _extract_pdf(data: bytes, order_with_model: bool = False,
                 layout: dict | None = None) -> tuple[str, int, list[int], list[int]]:
    """Return (full_text, page_count, ocr_pages, ocr_skipped_pages). Fills *layout* with how
    many pages used each reading-order method."""
    global _TESSERACT_OK

    import pdfplumber

    from .layout import ModelOrderer
    orderer = ModelOrderer() if order_with_model else None
    layout = layout if layout is not None else {}

    if _TESSERACT_OK is None:
        _TESSERACT_OK = _tesseract_available()
        if not _TESSERACT_OK:
            logger.warning(
                "Tesseract not found — OCR fallback disabled. "
                "Install Tesseract to enable OCR for image-only pages."
            )

    pages_text: list[str] = []
    ocr_pages: list[int] = []
    ocr_skipped: list[int] = []

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            page_count = len(pdf.pages)
            for i, page in enumerate(pdf.pages, start=1):
                text = _page_text(page, orderer, layout)
                if len(text.strip()) < OCR_MIN_CHARS:
                    if _TESSERACT_OK:
                        try:
                            img = page.to_image(resolution=150).original
                            text = _ocr_page(img)
                            ocr_pages.append(i)
                        except Exception as exc:
                            logger.warning("OCR failed on page %d: %s", i, exc)
                            ocr_skipped.append(i)
                    else:
                        ocr_skipped.append(i)
                pages_text.append(text)
    except Exception as exc:
        raise ExtractionError(f"Could not read PDF: {exc}") from exc

    return "\n\n".join(pages_text), page_count, ocr_pages, ocr_skipped


def _extract_text_file(data: bytes) -> tuple[str, int, list[int], list[int]]:
    """Decode plain text, trying UTF-8 then latin-1 as fallback."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except UnicodeDecodeError as exc:
            raise ExtractionError(f"Could not decode text file: {exc}") from exc
    return text, 1, [], []


# ─── Public API ───────────────────────────────────────────────────────────────

class IngestionError(ValueError):
    """Raised for validation failures (bad type, oversized). Maps to 4xx."""

class ExtractionError(RuntimeError):
    """Raised when a valid file cannot be parsed. Maps to 422."""


def ingest(data: bytes, filename: str, content_type: str, *,
           order_with_model: bool = False) -> ExtractionResult:
    """Validate, extract text, and return an ExtractionResult.

    order_with_model: let a model decide the reading order of multi-column PDF pages. Slower
    (one small call per such page), so it's used in background processing, not on upload.

    Raises:
        IngestionError: unsupported type or file too large (caller should 4xx).
        ExtractionError: file is valid type but unreadable (caller should 422).
    """
    size = len(data)

    # Size check
    if size > MAX_FILE_BYTES:
        mb = size / 1024 / 1024
        raise IngestionError(f"File exceeds the 25 MB limit ({mb:.1f} MB received).")

    # Type check — use both MIME type and extension
    import os
    ext = os.path.splitext(filename)[1].lower()
    mime_ok = any(content_type.startswith(p) for p in SUPPORTED_MIME_PREFIXES)
    ext_ok = ext in SUPPORTED_EXTENSIONS

    if not mime_ok and not ext_ok:
        raise IngestionError(
            f"Unsupported file type '{content_type}' (extension: '{ext}'). "
            f"Accepted: PDF or plain text."
        )

    # Empty file check
    if size == 0:
        raise IngestionError("Uploaded file is empty.")

    # Extract
    layout: dict = {}
    if content_type == "application/pdf" or ext == ".pdf":
        text, page_count, ocr_pages, ocr_skipped = _extract_pdf(data, order_with_model, layout)
    else:
        text, page_count, ocr_pages, ocr_skipped = _extract_text_file(data)

    return ExtractionResult(
        filename=filename,
        content_type=content_type,
        size_bytes=size,
        page_count=page_count,
        char_count=len(text),
        ocr_pages=ocr_pages,
        ocr_skipped_pages=ocr_skipped,
        text=text,
        layout=layout,
    )
