"""
Extraction layer for the Stage-9 document-upload prefill feature.

Goal: turn an uploaded PDF or Word doc into clean, COMPLETE, reading-ORDER text
(Markdown) that we can hand to the LLM. We deliberately keep this layer "dumb":
it does not try to understand the document, it just faithfully serializes it.
The LLM does the understanding.

Design decisions (see RnD report for full reasoning):
  PDF  -> PyMuPDF (fitz) primary. Fast, accurate reading order via "blocks".
          pdfplumber used ONLY to recover tables (which plain text flattens).
          If a page yields ~no text, we flag it as image-only (OCR candidate).
  DOCX -> order-preserving walk of the document body XML so paragraphs AND
          tables come out interleaved in true reading order (python-docx alone
          loses this). mammoth is available as an HTML fallback if we ever want
          to preserve inline formatting.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import List

import fitz  # PyMuPDF
import pdfplumber
import docx
from docx.document import Document as _DocxDoc
from docx.table import Table as _DocxTable
from docx.text.paragraph import Paragraph as _DocxPara
from docx.oxml.text.paragraph import CT_P
from docx.oxml.table import CT_Tbl


@dataclass
class ExtractionResult:
    text: str                       # clean markdown, reading order
    page_count: int
    char_count: int
    needs_ocr: bool = False         # True if image-only pages detected
    warnings: List[str] = field(default_factory=list)
    method: str = ""


# --------------------------- PDF ---------------------------

def _table_to_md(rows) -> str:
    rows = [[(c or "").strip().replace("\n", " ") for c in r] for r in rows if r]
    if not rows:
        return ""
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join("---" for _ in rows[0]) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def extract_pdf(path: str) -> ExtractionResult:
    warnings: List[str] = []
    parts: List[str] = []
    needs_ocr = False

    doc = fitz.open(path)
    page_count = doc.page_count

    # Pull tables per-page with pdfplumber (coordinates -> structure).
    with pdfplumber.open(path) as pl:
        for i, page in enumerate(doc):
            # 1) Plain text in natural reading order (sorted blocks).
            page_text = page.get_text("text", sort=True).strip()

            # 2) Detect image-only / scanned pages.
            if len(page_text) < 15 and page.get_images():
                needs_ocr = True
                warnings.append(f"Page {i+1}: little/no extractable text — likely scanned (OCR needed).")

            # 3) Recover tables (plain text flattens them).
            tables_md = []
            try:
                for tbl in pl.pages[i].extract_tables():
                    md = _table_to_md(tbl)
                    if md:
                        tables_md.append(md)
            except Exception as e:  # pragma: no cover
                warnings.append(f"Page {i+1}: table extraction failed ({e}).")

            chunk = [f"\n<!-- page {i+1} -->\n", page_text]
            if tables_md:
                chunk.append("\n\n" + "\n\n".join(tables_md))
            parts.append("\n".join(c for c in chunk if c))

    doc.close()
    text = _normalize("\n\n".join(parts))
    return ExtractionResult(
        text=text, page_count=page_count, char_count=len(text),
        needs_ocr=needs_ocr, warnings=warnings, method="pymupdf+pdfplumber",
    )


# --------------------------- DOCX ---------------------------

def _iter_block_items(parent):
    """Yield paragraphs and tables in document order (true reading order)."""
    if isinstance(parent, _DocxDoc):
        parent_elm = parent.element.body
    else:
        parent_elm = parent._tc
    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield _DocxPara(child, parent)
        elif isinstance(child, CT_Tbl):
            yield _DocxTable(child, parent)


def extract_docx(path: str) -> ExtractionResult:
    d = docx.Document(path)
    parts: List[str] = []
    for block in _iter_block_items(d):
        if isinstance(block, _DocxPara):
            txt = block.text.strip()
            if not txt:
                continue
            style = (block.style.name or "").lower()
            if "heading 1" in style:
                parts.append(f"# {txt}")
            elif "heading 2" in style:
                parts.append(f"## {txt}")
            elif "heading" in style:
                parts.append(f"### {txt}")
            else:
                parts.append(txt)
        elif isinstance(block, _DocxTable):
            rows = [[c.text.strip() for c in row.cells] for row in block.rows]
            md = _table_to_md(rows)
            if md:
                parts.append(md)
    text = _normalize("\n\n".join(parts))
    return ExtractionResult(
        text=text, page_count=1, char_count=len(text),
        needs_ocr=False, warnings=[], method="python-docx (order-preserving walk)",
    )


# --------------------------- shared ---------------------------

def _normalize(s: str) -> str:
    lines = [ln.rstrip() for ln in s.replace("\r", "").split("\n")]
    out, blank = [], 0
    for ln in lines:
        if ln.strip() == "":
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(ln)
    return "\n".join(out).strip()


def extract(path: str) -> ExtractionResult:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            return extract_pdf(path)
        if ext in (".docx", ".doc"):
            return extract_docx(path)
        raise ValueError(f"Unsupported file type: {ext}")
    except Exception as e:
        msg = str(e).lower()
        # Scenario L: password-protected PDFs
        if "password" in msg or "encrypted" in msg or "decryptionerror" in msg:
            return ExtractionResult(
                text="", page_count=0, char_count=0, needs_ocr=False,
                warnings=[f"File is password-protected and cannot be opened. "
                           f"Please upload an unlocked version."],
                method="failed:encrypted",
            )
        # Scenario L: corrupt/unreadable files
        return ExtractionResult(
            text="", page_count=0, char_count=0, needs_ocr=False,
            warnings=[f"File could not be parsed ({type(e).__name__}: {e}). "
                       f"Please re-upload a readable version."],
            method="failed:corrupt",
        )


if __name__ == "__main__":
    import sys, json
    for f in sys.argv[1:]:
        r = extract(f)
        print(f"\n===== {os.path.basename(f)} =====")
        print(f"method={r.method} pages={r.page_count} chars={r.char_count} "
              f"needs_ocr={r.needs_ocr} warnings={r.warnings}")
        print("-" * 60)
        print(r.text)
