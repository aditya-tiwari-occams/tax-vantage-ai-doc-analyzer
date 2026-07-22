"""
Proof-of-work: ground the LLM's per-field citations back to a page number and
nearby section heading in the extracted source text, and render a human
-readable Markdown report a reviewer can use to verify each field.

Pipeline shape
--------------
1. prefill.py's schema asks the model for a short VERBATIM quote per field
   (see CITED_FIELDS / PROJECT_JSON_SCHEMA["citations"]).
2. pipeline.py calls ground_citations() right after each LLM call returns,
   while we still have the exact `sec.text` the model was looking at. This
   locates each quote in the source text (tolerant of whitespace differences)
   and attaches {page, section, verified, source_file}.
3. Cross-document / cross-chunk merges (pipeline._merge_field) carry the
   winning field's citation along so provenance survives deduplication.
4. render_project_markdown() / render_batch_markdown() turn the grounded
   citations into a downloadable "Proof of Work" report.

Design choice: page + verbatim quote, not line number. PDF text extraction
reflows/collapses whitespace (see extract.py / segment.py trim_boilerplate),
so a computed "line number" would not reliably match what a human sees when
opening the actual file. A page number + an exact quote is stable and lets a
reviewer Ctrl+F the source in seconds. If a quote can't be matched verbatim
(model paraphrased), we mark it `verified: False` and omit the page rather
than guessing.
"""
from __future__ import annotations
import re
from typing import Optional

_PAGE_RE = re.compile(r"<!--\s*page\s*(\d+)\s*-->")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)

# Kept in sync with prefill.CITED_FIELDS.
CITED_FIELDS = [
    "project_title", "tax_year", "contract_type", "description",
    "total_man_hours", "employees_completing_research", "supplies_used",
    "solutions_alternatives_considered", "technical_challenges_uncertainties",
]

FIELD_LABELS = {
    "project_title": "Project Title",
    "tax_year": "Tax Year",
    "contract_type": "Contract Type",
    "description": "Description",
    "total_man_hours": "Total Man Hours",
    "employees_completing_research": "Employees Completing Research",
    "supplies_used": "Supplies Used",
    "solutions_alternatives_considered": "Solutions / Alternatives Considered",
    "technical_challenges_uncertainties": "Technical Challenges / Uncertainties",
}


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _find_quote(quote: str, source_text: str) -> Optional[int]:
    """Return the character offset of `quote` inside `source_text`, tolerant
    of whitespace and case differences, or None if it can't be found."""
    if not quote or not quote.strip():
        return None

    # 1) exact match — fast path, and the common case for numbers/short spans.
    idx = source_text.find(quote)
    if idx != -1:
        return idx

    # 2) whitespace-normalized match (model may have altered line wrapping).
    norm_quote = _normalize_ws(quote)
    if not norm_quote:
        return None
    norm_source = _normalize_ws(source_text)
    n_idx = norm_source.find(norm_quote)
    # 3) case-insensitive normalized match as a last resort.
    if n_idx == -1:
        n_idx = norm_source.lower().find(norm_quote.lower())
    if n_idx == -1:
        return None

    # Map the normalized offset back to an approximate offset in the original
    # text by walking both strings in lockstep, collapsing whitespace runs.
    orig_i, norm_i, prev_ws = 0, 0, True
    while orig_i < len(source_text) and norm_i < n_idx:
        c = source_text[orig_i]
        if c.isspace():
            if not prev_ws:
                norm_i += 1
            prev_ws = True
        else:
            norm_i += 1
            prev_ws = False
        orig_i += 1
    return orig_i


def ground_quote(quote: Optional[str], source_text: str) -> dict:
    """Locate one citation's quote inside `source_text`.

    Returns {quote, page, section, verified}:
      - verified=True  -> quote found verbatim (mod whitespace/case); page and
                          section (nearest preceding markdown heading, if any)
                          are populated.
      - verified=False -> quote missing/empty, or could not be matched. page
                          and section are left None rather than guessed.
    """
    if not quote or not str(quote).strip():
        return {"quote": None, "page": None, "section": None, "verified": False}

    offset = _find_quote(str(quote), source_text)
    if offset is None:
        return {"quote": quote, "page": None, "section": None, "verified": False}

    page = None
    for m in _PAGE_RE.finditer(source_text, 0, offset + 1):
        page = int(m.group(1))

    section = None
    for m in _HEADING_RE.finditer(source_text[:offset]):
        section = m.group(2).strip()

    return {"quote": quote, "page": page, "section": section, "verified": True}


def ground_citations(citations: Optional[dict], source_text: str, source_file: str) -> dict:
    """Ground every field's raw quote (from the LLM) against `source_text`,
    attaching page/section/verified + which uploaded file it came from."""
    grounded = {}
    for field in CITED_FIELDS:
        quote = (citations or {}).get(field)
        g = ground_quote(quote, source_text)
        g["source_file"] = source_file
        grounded[field] = g
    return grounded


def _escape_md(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def render_project_markdown(project: dict) -> str:
    """Render one project's citations as a Markdown 'Proof of Work' table."""
    title = project.get("project_title") or "Untitled Project"
    lines = [f"## {title}", ""]
    lines.append("| Field | Value | Source File | Page | Section | Quote |")
    lines.append("|---|---|---|---|---|---|")
    citations = project.get("citations") or {}
    for field in CITED_FIELDS:
        label = FIELD_LABELS.get(field, field)
        value = project.get(field)
        value_s = _escape_md("" if value is None else str(value)) or "—"
        c = citations.get(field) or {}
        src = c.get("source_file") or project.get("_source_file") or "—"
        # NOTE: verified and page are independent — a quote can be verified
        # (found verbatim in the source text) with no page number when the
        # source is a DOCX (extract_docx never emits <!-- page N --> markers,
        # so there's nothing for ground_quote() to attach). Don't conflate
        # "no page available" with "not verified" — that would mislabel every
        # correctly-verified DOCX citation as unverified.
        if c.get("verified"):
            page = f"p. {c['page']}" if c.get("page") else "verified"
        elif c.get("quote"):
            page = "unverified"
        else:
            page = "—"
        section = _escape_md(c.get("section") or "") or "—"
        quote = _escape_md(c.get("quote") or "") or "—"
        if len(quote) > 160:
            quote = quote[:157] + "..."
        lines.append(f"| {label} | {value_s} | {src} | {page} | {section} | \u201c{quote}\u201d |")
    lines.append("")
    return "\n".join(lines)


def render_batch_markdown(projects: list) -> str:
    """Render a full 'Proof of Work' report for a batch of projects."""
    parts = [
        "# Proof of Work \u2014 Source Verification Report",
        "",
        "Each row below shows exactly where an extracted value was found in the "
        "uploaded source document(s), so it can be verified against the original "
        "file. \"unverified\" means the AI's cited quote could not be matched "
        "verbatim in the source text \u2014 double-check that field manually.",
        "",
    ]
    for p in projects:
        parts.append(render_project_markdown(p))
    return "\n".join(parts)
