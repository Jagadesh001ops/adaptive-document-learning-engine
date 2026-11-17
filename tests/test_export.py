"""Tests for engine/export.py — Word-style PDF and DOCX exports, and Anki decks."""
import io
import json
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


def _make_run(
    id_=1,
    goal="Summarise Chapter 5",
    summary="Print culture transformed society.",
    concepts=None,
    quiz_items=None,
    output_json=None,
):
    run = MagicMock()
    run.id = id_
    run.goal = goal
    run.summary = summary
    data = {}
    if concepts is not None:
        data["concepts"] = concepts
    if quiz_items is not None:
        data["quiz_items"] = quiz_items
    run.output_json = output_json if output_json is not None else (
        json.dumps(data) if data else None
    )
    return run


# ── PDF and Word (real renderers) ────────────────────────────────────────────

def _pdf_text(data: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _docx(data: bytes):
    import docx
    d = docx.Document(io.BytesIO(data))
    return [(p.style.name, p.text) for p in d.paragraphs], d


QUIZ = [{"question": "Who invented the printing press?",
         "options": {"A": "Gutenberg", "B": "Polo", "C": "Luther", "D": "Erasmus"},
         "answer": "A", "explanation": "Gutenberg built it around 1440."}]


def test_run_pdf_includes_summary_concepts_quiz_and_answer_key():
    import engine.export as exp
    run = _make_run(summary=None, output_json=json.dumps({
        "summary": "- Print spread ideas\n- Literacy grew", "concepts": ["Gutenberg press"], "quiz": QUIZ}))
    run.summary = None
    data = exp.generate_pdf(run)
    assert data.startswith(b"%PDF")
    text = _pdf_text(data)
    for expected in ("Summary", "Print spread ideas", "Key concepts", "Gutenberg press",
                     "Who invented the printing press?", "A.", "Answer key", "Page 2"):
        assert expected in text, expected


def test_run_pdf_reads_older_quiz_keys_and_json_strings():
    import engine.export as exp
    for key in ("quiz_items", "verified_quiz_items"):
        run = _make_run(output_json=json.dumps({key: json.dumps(QUIZ)}))
        assert "Who invented the printing press?" in _pdf_text(exp.generate_pdf(run))


def test_pdf_handles_markup_characters_and_empty_runs():
    import engine.export as exp
    run = _make_run(summary=f"Tags <b> & ampersands {chr(0x2011)} and **bold**", output_json="{}")
    assert "Tags <b> & ampersands" in _pdf_text(exp.generate_pdf(run))
    empty = _make_run(summary=None, output_json="{}")
    empty.summary = None
    assert exp.generate_pdf(empty).startswith(b"%PDF")


def test_pdf_page_size_option():
    import pdfplumber

    import engine.export as exp
    data = exp.generate_pdf(_make_run(), page_size="letter")
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        assert round(pdf.pages[0].width) == 612


def test_pdf_falls_back_to_helvetica_without_truetype_fonts():
    import engine.export as exp
    with patch.dict(exp._font_cache, {"name": "Helvetica"}):
        run = _make_run(summary=f"Curly {chr(0x201C)}quotes{chr(0x201D)} and dashes {chr(0x2013)} here")
        assert 'Curly "quotes" and dashes - here' in _pdf_text(exp.generate_pdf(run))


TURNS = [
    {"turn": 1, "message": "Why did the Church fear print?", "blocks": [
        {"type": "answer", "answer": "It spread heresy.", "justification": "Passage 2 says so.",
         "citations": [{"chunk_id": 2, "quote": "feared printed books spread heresy"}],
         "status": "answered", "general_knowledge": "Outside the book: the Index lasted until 1966."},
        {"type": "quiz", "items": QUIZ}]},
    {"turn": 2, "message": "hello", "blocks": [], "reply": "Hi"},
    {"turn": 3, "message": "Summarize in 2 bullets", "blocks": [
        {"type": "summary", "text": "- One\n- Two"}, {"type": "quiz", "items": QUIZ}]},
]


@pytest.fixture(params=["installed", "Helvetica"])
def pdf_font(request):
    """Run with whatever TrueType font this machine has, and with the built-in fallback
    (what a server without Carlito or Calibri uses)."""
    import engine.export as exp
    if request.param == "Helvetica":
        with patch.dict(exp._font_cache, {"name": "Helvetica"}):
            yield request.param
    else:
        yield request.param


def test_session_pdf_lays_out_each_turn_with_one_answer_key(pdf_font):
    import engine.export as exp
    text = _pdf_text(exp.export_session("pdf", "My session", "print.pdf", TURNS))
    assert "?" not in text.replace("Who invented the printing press?", "").replace(
        "Why did the Church fear print?", ""), "no characters lost to the fallback font"
    assert "My session" in text and "print.pdf" in text
    assert "Why did the Church fear print?" in text and "From the document" in text
    assert "feared printed books spread heresy" in text and "Beyond the document" in text
    assert "hello" not in text, "turns without content are skipped"
    assert text.count("Who invented the printing press?") == 2
    assert "2. Who invented" in text, "questions are numbered across the whole session"
    assert text.count("Answer key") == 1


def test_session_docx_uses_word_styles_and_a_footer_page_number():
    import engine.export as exp
    paragraphs, d = _docx(exp.export_session("docx", "My session", "print.pdf", TURNS))
    styles = {text: style for style, text in paragraphs}
    assert styles["My session"] == "Title"
    assert styles["Why did the Church fear print?"] == "Heading 1"
    assert styles["Why"] == "Heading 2"
    assert styles["feared printed books spread heresy"] == "Quote"
    assert styles["One"] == "List Bullet"
    assert any(t.startswith("1.\tWho invented") for _, t in paragraphs)
    [table] = d.tables
    assert [c.text for c in table.rows[1].cells] == ["1", "A", "Gutenberg built it around 1440."]
    section = d.sections[0]
    assert round(section.page_width.mm) == 210 and section.left_margin.inches == 1
    assert "PAGE" in section.footer.paragraphs[0]._p.xml


def test_run_docx_and_filenames():
    import engine.export as exp
    paragraphs, _ = _docx(exp.generate_docx(_make_run(quiz_items=QUIZ)))
    assert ("Heading 1", "Answer key") in paragraphs
    assert exp.safe_filename("My Book (final).pdf") == "My-Book-final"
    assert exp.safe_filename("") == "study-session"


def test_text_blocks_split_paragraphs_and_lists():
    import engine.export as exp
    blocks = exp.text_blocks("Intro line\n\n- a\n* b\n1. one\n2) two\n## Heading text")
    assert blocks == [("p", "Intro line"), ("bullets", ["a", "b"]), ("numbered", ["one", "two"]),
                      ("p", "Heading text")]


def test_missing_renderers_raise_a_clear_error():
    import engine.export as exp
    with patch.dict(sys.modules, {"reportlab.platypus": None}), pytest.raises(RuntimeError):
        exp.render_pdf([("p", "x")])
    with patch.dict(sys.modules, {"docx": None}), pytest.raises(RuntimeError):
        exp.render_docx([("p", "x")])


# ── Anki — missing genanki ────────────────────────────────────────────────────

def test_generate_anki_raises_if_no_genanki():
    import engine.export as exp
    with patch.dict(sys.modules, {"genanki": None}), pytest.raises((RuntimeError, ImportError)):
        exp.generate_anki(_make_run())


# ── Anki — happy path with mocked genanki ────────────────────────────────────

def _mock_genanki():
    gn = ModuleType("genanki")
    gn.Model = MagicMock(return_value=MagicMock())
    gn.Deck = MagicMock(return_value=MagicMock())
    gn.Note = MagicMock(return_value=MagicMock())

    fake_pkg = MagicMock()

    def fake_write(buf):
        buf.write(b"PK\x03\x04fake-anki-bytes")
    fake_pkg.write_to_file.side_effect = fake_write
    gn.Package = MagicMock(return_value=fake_pkg)
    return {"genanki": gn}


def test_generate_anki_returns_bytes():
    import engine.export as exp
    with patch.dict(sys.modules, _mock_genanki()):
        assert isinstance(exp.generate_anki(_make_run()), bytes)


def test_generate_anki_adds_a_note_per_question_including_the_current_quiz_key():
    import engine.export as exp
    mocks = _mock_genanki()
    with patch.dict(sys.modules, mocks):
        exp.generate_anki(_make_run(output_json=json.dumps({"quiz": QUIZ * 2})))
    assert mocks["genanki"].Note.call_count == 2


def test_generate_anki_empty_quiz_items():
    import engine.export as exp
    with patch.dict(sys.modules, _mock_genanki()):
        assert isinstance(exp.generate_anki(_make_run(quiz_items=[])), bytes)


def test_generate_anki_quiz_items_as_json_string():
    import engine.export as exp
    mocks = _mock_genanki()
    with patch.dict(sys.modules, mocks):
        exp.generate_anki(_make_run(output_json=json.dumps({"quiz_items": json.dumps(QUIZ)})))
    assert mocks["genanki"].Note.call_count == 1


def test_real_anki_deck():
    import engine.export as exp
    data = exp.generate_anki(_make_run(quiz_items=QUIZ))
    assert data[:2] == b"PK"
