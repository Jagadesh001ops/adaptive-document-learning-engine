"""Exports: Word-style PDF and DOCX for study sessions and runs, and Anki decks for quizzes.

Content is first laid out as a list of simple blocks (title, headings, paragraphs, lists, quotes,
quiz questions, answer key), then rendered by either the PDF or the DOCX writer, so both files
have the same structure.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

APP_NAME = "Adaptive Document Learning Engine"

Block = Tuple  # ("title", text) | ("subtitle", text) | ("h1", text) | ("h2", text) | ("p", text)
               # | ("bullets", [text]) | ("numbered", [text]) | ("quote", text) | ("note", text)
               # | ("question", n, text, {letter: option}) | ("pagebreak",)
               # | ("answer_key", [(n, letter, explanation)])


# ── layout ────────────────────────────────────────────────────────────────────

_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\u2013)\s+(.*)")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)")


def text_blocks(text: str) -> List[Block]:
    """Split model text into paragraphs, bullet lists and numbered lists."""
    blocks: List[Block] = []
    for line in (text or "").splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        for kind, pattern in (("bullets", _BULLET), ("numbered", _NUMBERED)):
            m = pattern.match(line)
            if m:
                if blocks and blocks[-1][0] == kind:
                    blocks[-1][1].append(m.group(1))
                else:
                    blocks.append((kind, [m.group(1)]))
                break
        else:
            blocks.append(("p", line.strip()))
    return blocks


def _quiz_blocks(items: list, start: int, key: list) -> List[Block]:
    blocks: List[Block] = []
    for i, item in enumerate(items, start):
        blocks.append(("question", i, str(item.get("question", "")), dict(item.get("options") or {})))
        key.append((i, str(item.get("answer", "")), str(item.get("explanation", ""))))
    return blocks


def _answer_blocks(block: dict) -> List[Block]:
    out: List[Block] = text_blocks(block.get("answer", ""))
    if block.get("justification"):
        out.append(("h2", "Why"))
        out += text_blocks(block["justification"])
    quotes = [c.get("quote") for c in block.get("citations") or [] if c.get("quote")]
    if quotes:
        out.append(("h2", "From the document"))
        out += [("quote", q) for q in quotes]
    if block.get("general_knowledge"):
        out.append(("h2", "Beyond the document"))
        out += text_blocks(block["general_knowledge"])
    if block.get("status") == "not_in_document":
        out.append(("note", "The document doesn't answer this directly."))
    return out


_HEADINGS = {"summary": "Summary", "concepts": "Key concepts", "quiz": "Questions"}


def session_layout(title: str, document_name: str, turns: Iterable[dict]) -> List[Block]:
    """Lay out study-session turns: each turn's request as a heading, then what it produced."""
    blocks: List[Block] = [("title", title),
                           ("subtitle", f"{document_name} \u00b7 {datetime.now():%d %B %Y}")]
    key: list = []
    question_no = 1
    for t in turns:
        content = [b for b in t.get("blocks") or [] if b.get("type") in ("answer", "summary", "concepts", "quiz")]
        if not content:
            continue
        blocks.append(("h1", t.get("message") or "Request"))
        for b in content:
            if b["type"] == "answer":
                blocks += _answer_blocks(b)
            elif b["type"] == "quiz":
                if len(content) > 1:
                    blocks.append(("h2", _HEADINGS["quiz"]))
                items = b.get("items") or []
                blocks += _quiz_blocks(items, question_no, key)
                question_no += len(items)
            else:
                if len(content) > 1:
                    blocks.append(("h2", _HEADINGS[b["type"]]))
                blocks += text_blocks(b.get("text", ""))
    if key:
        blocks += [("pagebreak",), ("h1", "Answer key"), ("answer_key", key)]
    return blocks


def _run_output(run) -> dict:
    data = {}
    if run.output_json:
        with contextlib.suppress(Exception):
            data = json.loads(run.output_json)
    return data if isinstance(data, dict) else {}


def _run_quiz(data: dict) -> list:
    for k in ("verified_quiz", "quiz", "quiz_items", "verified_quiz_items", "quiz_questions"):
        items = data.get(k)
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                items = None
        if isinstance(items, dict):
            items = items.get("items")
        if isinstance(items, list) and items:
            return [q for q in items if isinstance(q, dict)]
    return []


def run_layout(run) -> List[Block]:
    data = _run_output(run)
    blocks: List[Block] = [("title", f"Study kit \u00b7 Run #{run.id}"),
                           ("subtitle", run.goal or "")]
    summary = data.get("summary") or run.summary
    if summary:
        blocks += [("h1", "Summary"), *text_blocks(str(summary))]
    concepts = data.get("concepts")
    if concepts:
        blocks.append(("h1", "Key concepts"))
        blocks += [("bullets", [str(c) for c in concepts])] if isinstance(concepts, list) \
            else text_blocks(str(concepts))
    key: list = []
    quiz = _run_quiz(data)
    if quiz:
        blocks += [("h1", "Questions"), *_quiz_blocks(quiz, 1, key)]
        blocks += [("pagebreak",), ("h1", "Answer key"), ("answer_key", key)]
    return blocks


# ── PDF ───────────────────────────────────────────────────────────────────────

_FONT_DIRS = ["/usr/share/fonts/truetype/crosextra", "/usr/share/fonts/truetype/carlito",
              os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")]
_FONT_FILES = [  # Carlito has the same metrics as Calibri, Word's default font
    ("Carlito-Regular.ttf", "Carlito-Bold.ttf", "Carlito-Italic.ttf", "Carlito-BoldItalic.ttf"),
    ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
]
_font_cache: dict = {}

# Characters the built-in Helvetica can't show, for when no TrueType font is installed.
_ASCII = str.maketrans({0x2011: "-", 0x2010: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-",
                        0x2212: "-", 0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"',
                        0x2026: "...", 0x00A0: " ", 0x202F: " ", 0x2009: " "})


def _pdf_font() -> str:
    """Register a Word-like TrueType font if one is installed; fall back to Helvetica."""
    if "name" in _font_cache:
        return _font_cache["name"]
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    name = "Helvetica"
    for files in _FONT_FILES:
        for d in _FONT_DIRS:
            paths = [os.path.join(d, f) for f in files]
            if all(os.path.exists(p) for p in paths):
                try:
                    for suffix, path in zip(("", "-Bold", "-Italic", "-BoldItalic"), paths, strict=True):
                        pdfmetrics.registerFont(TTFont(f"Body{suffix}", path))
                    addMapping("Body", 0, 0, "Body")
                    addMapping("Body", 1, 0, "Body-Bold")
                    addMapping("Body", 0, 1, "Body-Italic")
                    addMapping("Body", 1, 1, "Body-BoldItalic")
                    name = "Body"
                except Exception:
                    continue
                break
        if name != "Helvetica":
            break
    _font_cache["name"] = name
    return name


def _plain(text: str) -> str:
    return text.translate(_ASCII).encode("latin-1", "replace").decode("latin-1")


def _markup(text: str, font: str) -> str:
    """Escape text for reportlab and turn **bold** into <b>."""
    from xml.sax.saxutils import escape
    # Non-breaking hyphens are missing from most fonts, including Calibri.
    text = text.replace(chr(0x2011), "-")
    if font == "Helvetica":
        text = _plain(text)
    text = escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def render_pdf(blocks: List[Block], *, page_size: str = "A4", footer: str = APP_NAME) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_JUSTIFY
        from reportlab.lib.pagesizes import A4, letter
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            KeepTogether,
            ListFlowable,
            ListItem,
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:
        raise RuntimeError("reportlab not installed — run: pip install reportlab") from exc

    font = _pdf_font()
    bold = "Body-Bold" if font == "Body" else "Helvetica-Bold"
    italic = "Body-Italic" if font == "Body" else "Helvetica-Oblique"
    ink, muted, accent = colors.HexColor("#1F1F1F"), colors.HexColor("#595959"), colors.HexColor("#1F3864")

    body = ParagraphStyle("body", fontName=font, fontSize=11, leading=15.5, spaceAfter=8,
                          alignment=TA_JUSTIFY, textColor=ink)
    styles = {
        "title": ParagraphStyle("title", parent=body, fontName=font, fontSize=26, leading=31,
                                alignment=0, textColor=accent, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", parent=body, fontSize=12, alignment=0,
                                   textColor=muted, spaceAfter=18),
        "h1": ParagraphStyle("h1", parent=body, fontName=bold, fontSize=16, leading=20, alignment=0,
                             textColor=accent, spaceBefore=16, spaceAfter=8, keepWithNext=1),
        "h2": ParagraphStyle("h2", parent=body, fontName=bold, fontSize=13, leading=17, alignment=0,
                             textColor=accent, spaceBefore=10, spaceAfter=4, keepWithNext=1),
        "p": body,
        "item": ParagraphStyle("item", parent=body, spaceAfter=3),
        "quote": ParagraphStyle("quote", parent=body, fontName=italic, leftIndent=0.4 * inch,
                                rightIndent=0.4 * inch, textColor=muted, alignment=0),
        "note": ParagraphStyle("note", parent=body, fontName=italic, textColor=muted, alignment=0),
        "question": ParagraphStyle("question", parent=body, fontName=bold, alignment=0,
                                   leftIndent=0.3 * inch, firstLineIndent=-0.3 * inch, spaceAfter=3),
        "option": ParagraphStyle("option", parent=body, alignment=0, leftIndent=0.6 * inch,
                                 firstLineIndent=-0.25 * inch, spaceAfter=1),
    }

    def para(text, style):
        return Paragraph(_markup(str(text), font), styles[style])

    def lst(items, numbered):
        kwargs = {"bulletType": "1", "bulletFormat": "%s."} if numbered else \
                 {"bulletType": "bullet", "start": chr(0x2022)}
        return ListFlowable([ListItem(para(t, "item"), leftIndent=0.3 * inch) for t in items],
                            leftIndent=0.3 * inch, bulletFontName=font, bulletFontSize=10 if numbered else 9,
                            spaceAfter=8, **kwargs)

    story = []
    for b in blocks:
        kind = b[0]
        if kind in ("title", "subtitle", "h1", "h2", "p", "quote", "note"):
            if b[1]:
                story.append(para(b[1], kind))
        elif kind in ("bullets", "numbered"):
            story.append(lst(b[1], kind == "numbered"))
        elif kind == "question":
            _, n, text, options = b
            parts = [para(f"{n}.\u2002{text}", "question")]
            parts += [para(f"{letter}.\u2002{opt}", "option") for letter, opt in options.items()]
            parts.append(Spacer(1, 8))
            story.append(KeepTogether(parts))
        elif kind == "pagebreak":
            story.append(PageBreak())
        elif kind == "answer_key":
            rows = [[para("No.", "item"), para("Answer", "item"), para("Explanation", "item")]]
            rows += [[para(str(n), "item"), para(letter, "item"), para(expl, "item")]
                     for n, letter, expl in b[1]]
            table = Table(rows, colWidths=[0.5 * inch, 0.8 * inch, None], repeatRows=1)
            table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, 0), bold),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, accent),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#BFBFBF")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
            story.append(table)

    def draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 9)
        canvas.setFillColor(muted)
        width = doc.pagesize[0]
        text = _plain(footer) if font == "Helvetica" else footer
        canvas.drawString(doc.leftMargin, 0.55 * inch, text)
        canvas.drawRightString(width - doc.rightMargin, 0.55 * inch, f"Page {doc.page}")
        canvas.restoreState()

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=letter if page_size.lower() == "letter" else A4,
                            leftMargin=inch, rightMargin=inch, topMargin=inch, bottomMargin=inch,
                            title=next((b[1] for b in blocks if b[0] == "title"), APP_NAME),
                            author=APP_NAME)
    doc.build(story or [Spacer(1, 1)], onFirstPage=draw_footer, onLaterPages=draw_footer)
    return out.getvalue()


# ── DOCX ──────────────────────────────────────────────────────────────────────

def render_docx(blocks: List[Block], *, page_size: str = "A4", footer: str = APP_NAME) -> bytes:
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Inches, Mm, Pt, RGBColor
    except ImportError as exc:
        raise RuntimeError("python-docx not installed — run: pip install python-docx") from exc

    doc = Document()
    section = doc.sections[0]
    if page_size.lower() == "letter":
        section.page_width, section.page_height = Inches(8.5), Inches(11)
    else:
        section.page_width, section.page_height = Mm(210), Mm(297)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, side, Inches(1))

    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "Calibri", Pt(11)
    normal.paragraph_format.space_after = Pt(8)
    normal.paragraph_format.line_spacing = 1.15
    accent = RGBColor(0x1F, 0x38, 0x64)
    for name, size in (("Title", 26), ("Heading 1", 16), ("Heading 2", 13)):
        st = doc.styles[name]
        st.font.name, st.font.size, st.font.color.rgb = "Calibri", Pt(size), accent

    def add_text(paragraph, text):
        for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", str(text))):
            if part:
                paragraph.add_run(part).bold = bool(i % 2)
        return paragraph

    def body(text, style=None):
        p = add_text(doc.add_paragraph(style=style), text)
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        return p

    for b in blocks:
        kind = b[0]
        if kind == "title":
            doc.add_paragraph(b[1], style="Title")
        elif kind == "subtitle":
            p = doc.add_paragraph()
            run = p.add_run(b[1])
            run.font.size, run.font.color.rgb = Pt(12), RGBColor(0x59, 0x59, 0x59)
            p.paragraph_format.space_after = Pt(18)
        elif kind in ("h1", "h2"):
            doc.add_heading(b[1], level=1 if kind == "h1" else 2)
        elif kind == "p":
            body(b[1])
        elif kind in ("bullets", "numbered"):
            style = "List Number" if kind == "numbered" else "List Bullet"
            for item in b[1]:
                add_text(doc.add_paragraph(style=style), item)
        elif kind in ("quote", "note"):
            p = doc.add_paragraph(style="Quote" if kind == "quote" else None)
            run = p.add_run(b[1])
            run.italic = True
            if kind == "quote":
                p.paragraph_format.left_indent = p.paragraph_format.right_indent = Inches(0.4)
        elif kind == "question":
            _, n, text, options = b
            p = add_text(doc.add_paragraph(), f"{n}.\t{text}")
            for r in p.runs:
                r.bold = True
            p.paragraph_format.left_indent, p.paragraph_format.first_line_indent = Inches(0.3), Inches(-0.3)
            p.paragraph_format.keep_with_next, p.paragraph_format.space_after = True, Pt(3)
            for j, (letter, opt) in enumerate(options.items()):
                op = add_text(doc.add_paragraph(), f"{letter}.\t{opt}")
                op.paragraph_format.left_indent = Inches(0.6)
                op.paragraph_format.first_line_indent = Inches(-0.25)
                op.paragraph_format.space_after = Pt(8 if j == len(options) - 1 else 1)
                op.paragraph_format.keep_with_next = j < len(options) - 1
        elif kind == "pagebreak":
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        elif kind == "answer_key":
            table = doc.add_table(rows=1, cols=3)
            table.style = "Light List Accent 1"
            for cell, label in zip(table.rows[0].cells, ("No.", "Answer", "Explanation"), strict=True):
                cell.text = label
            for n, letter, expl in b[1]:
                cells = table.add_row().cells
                cells[0].text, cells[1].text, cells[2].text = str(n), letter, expl
            for row in table.rows:
                row.cells[0].width, row.cells[1].width, row.cells[2].width = Inches(0.5), Inches(0.8), Inches(5.2)

    # Footer: app name on the left, page number on the right.
    fp = section.footer.paragraphs[0]
    fp.text = f"{footer}\t\tPage "
    run = fp.add_run()
    for tag, attr in (("w:fldChar", {"w:fldCharType": "begin"}), ("w:instrText", None),
                      ("w:fldChar", {"w:fldCharType": "end"})):
        el = OxmlElement(tag)
        if attr:
            for k, v in attr.items():
                el.set(qn(k), v)
        else:
            el.set(qn("xml:space"), "preserve")
            el.text = "PAGE"
        run._r.append(el)
    for r in fp.runs:
        r.font.size, r.font.color.rgb = Pt(9), RGBColor(0x59, 0x59, 0x59)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# ── public helpers ────────────────────────────────────────────────────────────

def generate_pdf(run, page_size: str = "A4") -> bytes:
    """PDF for a workflow run."""
    return render_pdf(run_layout(run), page_size=page_size)


def generate_docx(run, page_size: str = "A4") -> bytes:
    """Word document for a workflow run."""
    return render_docx(run_layout(run), page_size=page_size)


def export_session(fmt: str, title: str, document_name: str, turns: List[dict],
                   page_size: str = "A4") -> bytes:
    blocks = session_layout(title, document_name, turns)
    footer = f"{document_name} \u00b7 {APP_NAME}"
    render = render_docx if fmt == "docx" else render_pdf
    return render(blocks, page_size=page_size, footer=footer)


def safe_filename(name: Optional[str], fallback: str = "study-session") -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.splitext(name or "")[0]).strip("-._")
    return (base or fallback)[:60]


# ── Anki export ───────────────────────────────────────────────────────────────

def generate_anki(run) -> bytes:
    """Return .apkg bytes for a workflow run using genanki."""
    try:
        import genanki
    except ImportError as exc:
        raise RuntimeError(
            "genanki not installed — run: pip install genanki"
        ) from exc

    quiz_items = _run_quiz(_run_output(run))

    deck_id = abs(hash(f"workflow-{run.id}")) % (10**10)
    model_id = abs(hash("ai-workflow-engine-quiz-model")) % (10**10)

    model = genanki.Model(
        model_id,
        "Study Quiz",
        fields=[
            {"name": "Question"},
            {"name": "Options"},
            {"name": "Answer"},
            {"name": "Explanation"},
        ],
        templates=[{
            "name": "Card 1",
            "qfmt": "{{Question}}<br><br>{{Options}}",
            "afmt": '{{FrontSide}}<hr id="answer"><b>Answer: {{Answer}}</b><br>{{Explanation}}',
        }],
    )

    deck = genanki.Deck(deck_id, f"{APP_NAME} — Run #{run.id}")

    for item in quiz_items:
        opts = item.get("options", {})
        opts_html = "<br>".join(f"{k}. {v}" for k, v in opts.items())
        note = genanki.Note(
            model=model,
            fields=[
                item.get("question", ""),
                opts_html,
                item.get("answer", ""),
                item.get("explanation", ""),
            ],
        )
        deck.add_note(note)

    pkg = genanki.Package(deck)
    buf = io.BytesIO()
    pkg.write_to_file(buf)
    return buf.getvalue()
