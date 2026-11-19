"""Reading order of PDF pages: blocks from whitespace, model-chosen order, geometric fallback."""
import io
from unittest.mock import patch

import pytest

from engine import layout
from engine.ingestion import ingest

PAGE_W, PAGE_H = 612, 792


def _pdf(draw) -> bytes:
    """Build a one-page PDF; draw(c, line) places text, line(x, y_from_top, text, size=11)."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))

    def line(x, y, text, size=11):
        c.setFont("Helvetica", size)
        c.drawString(x, PAGE_H - y, text)

    draw(c, line)
    c.save()
    return buf.getvalue()


def _text(data, **kw):
    result = ingest(data=data, filename="t.pdf", content_type="application/pdf", **kw)
    return result.text, result.layout


MAIN = ["The Roman Church, troubled by such effects of",
        "popular readings and questionings of faith, imposed",
        "severe controls over publishers and booksellers and",
        "began to maintain an Index of Prohibited Books."]
GLOSSARY = ["New words", "Satiety - The state of being", "fulfilled beyond the point", "of satisfaction"]


def _textbook(c, line):
    """A main column with a glossary box in the margin beside the middle of the paragraph."""
    line(60, 80, "Religious Debates and the Fear of Print", 14)
    for i, t in enumerate(MAIN * 3):
        line(60, 110 + i * 14, t)
    for i, t in enumerate(GLOSSARY):
        line(400, 140 + i * 13, t, 9)


# ── extraction ────────────────────────────────────────────────────────────────

def test_a_single_column_page_reads_top_to_bottom_without_the_model():
    def draw(c, line):
        for i, t in enumerate(["First line of the page.", "Second line follows.", "Third line ends it."]):
            line(72, 100 + i * 14, t)
    calls = []
    text, how = _text(_pdf(draw))
    assert text.index("First") < text.index("Second") < text.index("Third")
    assert how == {"single": 1} and calls == []


def test_margin_notes_are_not_spliced_into_the_paragraph():
    text, how = _text(_pdf(_textbook))
    flat = " ".join(text.split())
    assert " ".join(MAIN) in flat, "the paragraph's sentences stay whole"
    assert flat.index("Index of Prohibited Books") < flat.index("New words"), "the note comes after"
    assert how == {"geometric": 1}


def test_two_columns_are_read_one_after_the_other():
    left = [f"Left column sentence {i} continues here." for i in range(8)]
    right = [f"Right column sentence {i} goes on." for i in range(8)]

    def draw(c, line):
        line(72, 70, "A Two Column Paper Title", 16)
        for i, (a, b) in enumerate(zip(left, right, strict=True)):
            line(72, 110 + i * 14, a)
            line(330, 110 + i * 14, b)
    text, _ = _text(_pdf(draw))
    assert text.index("A Two Column") < text.index("Left column sentence 0")
    assert text.index("Left column sentence 7") < text.index("Right column sentence 0")


def test_rotated_watermark_text_is_dropped():
    def draw(c, line):
        line(72, 100, "Real content sits here on the page.")
        c.saveState()
        c.translate(30, 400)
        c.rotate(90)
        c.setFont("Helvetica", 10)
        c.drawString(0, 0, "Watermark Spine Title")
        c.restoreState()
    text, _ = _text(_pdf(draw))
    assert "Real content" in text and "Watermark" not in text


# ── model order ───────────────────────────────────────────────────────────────

def _blocks():
    import pdfplumber
    with pdfplumber.open(io.BytesIO(_pdf(_textbook))) as pdf:
        page = pdf.pages[0]
        return page.extract_words(extra_attrs=["size", "upright"]), page.width, page.height


def _ids(words):
    blocks = layout.page_blocks(words)
    title = next(b.id for b in blocks if "Religious" in b.text)
    main = next(b.id for b in blocks if "Roman Church" in b.text)
    note = next(b.id for b in blocks if "New words" in b.text)
    return blocks, title, main, note


def test_the_model_chooses_the_order_but_the_text_is_rebuilt_from_the_page():
    words, w, h = _blocks()
    blocks, title, main, note = _ids(words)
    seen = {}

    def orderer(bl, width, height, default):
        seen.update(blocks=len(bl), default=default)
        return {"order": [title, main], "side_notes": [note], "skip": []}

    result = layout.order_page(words, w, h, orderer)
    assert result.method == "model" and seen["blocks"] == len(blocks)
    assert result.text.index("Religious") < result.text.index("Roman Church") < result.text.index("New words")
    assert sorted(seen["default"]) == sorted(b.id for b in blocks)


@pytest.mark.parametrize("answer", [
    {"order": [1], "side_notes": [], "skip": []},                 # a block left out
    {"order": [1, 1, 2, 3], "side_notes": [], "skip": []},        # a block used twice
    {"order": [99], "side_notes": [], "skip": []},                # unknown id
    {"order": "1,2,3"},                                           # wrong shape
    None,
])
def test_an_invalid_model_answer_falls_back_to_the_geometric_order(answer):
    words, w, h = _blocks()
    result = layout.order_page(words, w, h, lambda *a: answer)
    assert result.method == "geometric"
    flat = " ".join(result.text.split())
    assert " ".join(MAIN) in flat


def test_the_model_can_skip_page_furniture_but_not_content():
    words, w, h = _blocks()
    _, title, main, note = _ids(words)
    # Skipping the short title is allowed; skipping the long paragraph is not, so it's kept.
    result = layout.order_page(words, w, h, lambda *a: {"order": [note], "side_notes": [], "skip": [title, main]})
    assert "Religious" not in result.text and "Roman Church" in result.text


def test_a_model_error_falls_back_to_the_geometric_order():
    words, w, h = _blocks()

    def broken(*a):
        raise RuntimeError("network down")
    assert layout.order_page(words, w, h, broken).method == "geometric"


def test_the_orderer_waits_once_then_stops_calling_a_busy_model():
    from engine.qa import ModelBusy
    words, w, h = _blocks()
    blocks = layout.page_blocks(words)
    orderer = layout.ModelOrderer()
    with patch("engine.layout.model_order", side_effect=ModelBusy("busy")) as call, \
         patch("engine.layout.time.sleep") as sleep:
        assert orderer(blocks, w, h, []) is None
        assert orderer(blocks, w, h, []) is None
    assert call.call_count == 2 and sleep.call_count == 1 and orderer.gave_up


def test_model_order_sends_positions_and_previews():
    words, w, h = _blocks()
    blocks = layout.page_blocks(words)
    with patch("engine.qa.chat_text", return_value=('{"order": [1], "side_notes": [], "skip": []}', 10)) as chat:
        assert layout.model_order(blocks, w, h, [1, 2, 3]) == {"order": [1], "side_notes": [], "skip": []}
    prompt = chat.call_args.args[2]
    assert "Page size: 612 x 792" in prompt and "[1] x=" in prompt and "size=" in prompt
    long_text = "word " * 200
    assert " ... " in layout._preview(long_text) and len(layout._preview(long_text)) < 220


def test_ingest_uses_the_model_only_when_asked():
    data = _pdf(_textbook)
    with patch("engine.layout.model_order", return_value=None) as call:
        _text(data)
        assert call.call_count == 0
        _, how = _text(data, order_with_model=True)
    assert call.call_count == 1 and how == {"geometric": 1}, "an unusable answer falls back"


def test_model_ordering_needs_a_key_and_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("LAYOUT_MODEL", "1")
    assert layout.model_ordering_enabled()
    monkeypatch.setenv("LAYOUT_MODEL", "0")
    assert not layout.model_ordering_enabled()
    monkeypatch.setenv("LAYOUT_MODEL", "1")
    monkeypatch.delenv("GROQ_API_KEY")
    assert not layout.model_ordering_enabled()
