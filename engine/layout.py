"""Reading order for PDF pages with more than one column, margin notes or boxes.

pdfplumber's extract_text() reads each line across the whole page, so a margin note beside a
paragraph is spliced into the middle of its sentences. Instead:

1. Code cuts the page into blocks: words -> lines -> segments (split where a wide gap separates
   columns) -> blocks (segments stacked in the same column with normal line spacing). This
   relies only on whitespace, so it isn't tuned to any one book.
2. Pages whose blocks are all stacked in one column are simply read top to bottom.
3. For a page with blocks side by side, the order is decided by a model (PLANNER_MODEL). It sees
   each block's position and the start and end of its text, and returns block ids only: the
   main text in reading order, side notes (margin notes, glossary boxes, captions), and running
   headers or page numbers to skip. It never writes text, so nothing can be invented or lost.
4. Code rebuilds the page from the original text and checks every block is used exactly once.
   If the model's answer is invalid, or the model is unavailable, the page falls back to a
   geometric order (recursive XY-cut: split into bands at full-width gaps, then into columns).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from statistics import median
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

EXTRACTOR = "layout-1"   # stored on each document; older documents are re-processed on re-upload

NL = chr(10)
COLUMN_GAP = 1.5        # a horizontal gap wider than this many font sizes separates columns
LINE_GAP = 1.2          # a vertical gap larger than this many line heights ends a block
PREVIEW_START = 140     # characters of each block shown to the model
PREVIEW_END = 60
MAX_SKIP_CHARS = 80     # only short blocks (headers, page numbers) may be skipped
BUSY_WAIT = 20          # seconds to wait once when the model is rate-limited

Orderer = Callable[[List["Block"], float, float, List[int]], Optional[dict]]


@dataclass
class Block:
    id: int
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    lines: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return NL.join(self.lines)


# ── blocks ────────────────────────────────────────────────────────────────────

def _segments(words: List[dict]) -> List[dict]:
    """Group words into lines, and split each line where a wide gap separates columns."""
    rows: List[List[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        h = w["bottom"] - w["top"]
        if rows and abs(rows[-1][0]["top"] - w["top"]) <= 0.5 * h:
            rows[-1].append(w)
        else:
            rows.append([w])
    segs = []
    for row in rows:
        row.sort(key=lambda w: w["x0"])
        current = [row[0]]
        for w in row[1:]:
            size = w.get("size") or (w["bottom"] - w["top"])
            if w["x0"] - current[-1]["x1"] > COLUMN_GAP * size:
                segs.append(current)
                current = [w]
            else:
                current.append(w)
        segs.append(current)
    return [{"x0": min(w["x0"] for w in s), "x1": max(w["x1"] for w in s),
             "top": min(w["top"] for w in s), "bottom": max(w["bottom"] for w in s),
             "size": median(w.get("size") or (w["bottom"] - w["top"]) for w in s),
             "text": " ".join(w["text"] for w in s)} for s in segs]


def page_blocks(words: List[dict]) -> List[Block]:
    """Stack line segments into blocks: same column, normal line spacing, similar font size."""
    blocks: List[Block] = []
    for seg in sorted(_segments(words), key=lambda s: (s["top"], s["x0"])):
        height = seg["bottom"] - seg["top"]
        home = None
        for b in reversed(blocks):
            overlap = min(b.x1, seg["x1"]) - max(b.x0, seg["x0"])
            narrower = min(b.x1 - b.x0, seg["x1"] - seg["x0"]) or 1
            if (overlap / narrower >= 0.5 and 0 <= seg["top"] - b.bottom <= LINE_GAP * height + 1
                    and abs(seg["size"] - b.size) <= 1.5):
                home = b
                break
        if home:
            home.lines.append(seg["text"])
            home.x0, home.x1 = min(home.x0, seg["x0"]), max(home.x1, seg["x1"])
            home.bottom = max(home.bottom, seg["bottom"])
        else:
            blocks.append(Block(id=len(blocks) + 1, x0=seg["x0"], x1=seg["x1"], top=seg["top"],
                                bottom=seg["bottom"], size=seg["size"], lines=[seg["text"]]))
    return blocks


def side_by_side(blocks: List[Block]) -> bool:
    """True when some blocks share vertical space but not horizontal: more than one column."""
    for i, a in enumerate(blocks):
        for b in blocks[i + 1:]:
            if min(a.bottom, b.bottom) > max(a.top, b.top) and (a.x1 < b.x0 or b.x1 < a.x0):
                return True
    return False


# ── geometric order (fallback) ────────────────────────────────────────────────

def _split(blocks: List[Block], lo, hi) -> List[List[Block]]:
    groups: List[List[Block]] = []
    end = None
    for b in sorted(blocks, key=lo):
        if groups and lo(b) < end:
            groups[-1].append(b)
            end = max(end, hi(b))
        else:
            groups.append([b])
            end = hi(b)
    return groups


def xy_cut(blocks: List[Block]) -> List[Block]:
    """Bands at full-width horizontal gaps, then columns left to right within a band."""
    if len(blocks) <= 1:
        return list(blocks)
    bands = _split(blocks, lambda b: b.top, lambda b: b.bottom)
    if len(bands) > 1:
        return [b for band in bands for b in xy_cut(band)]
    cols = _split(blocks, lambda b: b.x0, lambda b: b.x1)
    if len(cols) > 1:
        return [b for col in cols for b in xy_cut(col)]
    return sorted(blocks, key=lambda b: (b.top, b.x0))


# ── model order ───────────────────────────────────────────────────────────────

_ORDER_SYSTEM = """You put the text blocks of one PDF page into reading order.

Each block has an id, its position in points (x from the left edge, y from the top), its font
size, and the start and end of its text. Return ONLY JSON:
{"order": [ids of the main text, in reading order],
 "side_notes": [ids of margin notes, glossary boxes, sidebars, figure captions and other text
                outside the main flow, in the order to read them],
 "skip": [ids of running headers, footers and page numbers]}

Rules:
- Every id appears exactly once across the three lists.
- When a sentence runs from one block into another, put those blocks next to each other.
- Main text usually runs down a column and then on to the next column; boxes and margin notes
  beside it are side notes, not part of the flow.
- Only skip page numbers and running headers or footers; never skip real content."""


def _preview(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= PREVIEW_START + PREVIEW_END + 5:
        return flat
    return f"{flat[:PREVIEW_START]} ... {flat[-PREVIEW_END:]}"


def model_order(blocks: List[Block], width: float, height: float, default: List[int]) -> Optional[dict]:
    """Ask PLANNER_MODEL for the reading order. Returns its parsed JSON, or raises."""
    import json

    from .config import PLANNER_MODEL
    from .qa import chat_text, parse_json
    lines = [f"Page size: {width:.0f} x {height:.0f} points. Geometric guess at the order: {default}"]
    for b in blocks:
        lines.append(f"[{b.id}] x={b.x0:.0f}-{b.x1:.0f} y={b.top:.0f}-{b.bottom:.0f} "
                     f"size={b.size:.1f} | {json.dumps(_preview(b.text), ensure_ascii=False)}")
    text, _ = chat_text(PLANNER_MODEL, _ORDER_SYSTEM, NL.join(lines), json_mode=True)
    return parse_json(text)


def _validated(answer: Optional[dict], blocks: List[Block]) -> Optional[tuple]:
    """(main ids, side note ids) if the answer uses every block exactly once, else None.

    Long blocks the model wanted to skip are kept as side notes: only headers and page numbers
    may be dropped.
    """
    if not isinstance(answer, dict):
        return None
    lists = []
    for key in ("order", "side_notes", "skip"):
        ids = answer.get(key) or []
        if not isinstance(ids, list):
            return None
        try:
            lists.append([int(i) for i in ids])
        except (TypeError, ValueError):
            return None
    main, side, skip = lists
    used = main + side + skip
    by_id = {b.id: b for b in blocks}
    if sorted(used) != sorted(by_id) or not main:
        return None
    side += [i for i in skip if len(by_id[i].text) > MAX_SKIP_CHARS]
    return main, side


# ── page text ─────────────────────────────────────────────────────────────────

@dataclass
class PageResult:
    text: str
    method: str          # "single" | "model" | "geometric"


def order_page(words: List[dict], width: float, height: float,
               orderer: Optional[Orderer] = None) -> PageResult:
    blocks = page_blocks(words)
    if not blocks:
        return PageResult("", "single")
    if not side_by_side(blocks):
        ordered = sorted(blocks, key=lambda b: (b.top, b.x0))
        return PageResult((NL * 2).join(b.text for b in ordered), "single")

    geometric = xy_cut(blocks)
    if orderer is not None:
        try:
            result = _validated(orderer(blocks, width, height, [b.id for b in geometric]), blocks)
        except Exception as exc:   # the geometric order is a sound fallback
            logger.warning("model reading order failed: %s", exc)
            result = None
        if result:
            by_id = {b.id: b for b in blocks}
            main, side = result
            return PageResult((NL * 2).join(by_id[i].text for i in main + side), "model")
    return PageResult((NL * 2).join(b.text for b in geometric), "geometric")


class ModelOrderer:
    """Calls the model page by page; after it stays busy, the rest of the document is ordered
    geometrically rather than waiting indefinitely."""

    def __init__(self):
        self.gave_up = False

    def __call__(self, blocks, width, height, default):
        from .qa import ModelBusy
        if self.gave_up:
            return None
        try:
            return model_order(blocks, width, height, default)
        except ModelBusy:
            time.sleep(BUSY_WAIT)
            try:
                return model_order(blocks, width, height, default)
            except ModelBusy:
                logger.warning("reading-order model stayed busy; using geometric order for the rest")
                self.gave_up = True
                return None


def model_ordering_enabled() -> bool:
    return os.getenv("LAYOUT_MODEL", "1") == "1" and bool(os.getenv("GROQ_API_KEY"))
