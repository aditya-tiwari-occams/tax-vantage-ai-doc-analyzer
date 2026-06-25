"""
Structure-aware segmentation + token budgeting for the Stage-9 prefill pipeline.

Why this exists
---------------
A single study can be 17-35 pages and a batch can be 10 files. Sending whole
documents to one LLM call is wrong on two counts:
  1. COST   - you pay for every token, including boilerplate wage tables.
  2. ACCURACY - long context suffers "lost in the middle": facts buried mid-doc
                get missed. Smaller, focused inputs extract far more reliably.

So we SEGMENT each document into per-project sections using the heading anchors
seen in real Occams studies, trim obvious boilerplate, and only fall back to
overlapping-chunk map-reduce when a section is both anchorless AND large.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import List

# ~4 chars per token is a good offline heuristic for English prose.
# Swap for tiktoken if you want exact counts (see estimate_tokens()).
CHARS_PER_TOKEN = 4
# A document up to this size goes to the LLM in ONE call (it segments projects
# itself). Above it, we chunk by size. ~12k tokens ≈ a dense ~15-page study after
# boilerplate trimming — comfortably inside the model's reliable attention zone,
# well below where "lost in the middle" degrades long-context recall.
MAX_SINGLE_CALL_TOKENS = 12000
CHUNK_TOKENS = 6000            # target size of each chunk when a doc is oversized
CHUNK_OVERLAP_TOKENS = 250     # overlap so a project split across a boundary survives


@dataclass
class Section:
    doc: str                 # source filename
    index: int               # section order within the batch
    title_hint: str          # best-guess title (the LLM may override)
    text: str
    token_est: int
    pages: str = ""          # e.g. "p3-p6" if page markers are present
    needs_chunking: bool = False
    chunks: List[str] = field(default_factory=list)
    # strategy: "whole_doc"  = small enough for one LLM call; the model returns the
    #                          full project array (it does the segmentation).
    #           "llm_detect" = oversized; LLM finds projects in each chunk and the
    #                          pipeline merges duplicates across chunks by title.
    strategy: str = "whole_doc"


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _page_span(text: str) -> str:
    pages = re.findall(r"<!--\s*page\s*(\d+)\s*-->", text)
    if not pages:
        return ""
    return f"p{pages[0]}" if len(pages) == 1 else f"p{pages[0]}-p{pages[-1]}"


def trim_boilerplate(text: str) -> str:
    """Collapse very long, repetitive line-item tables (e.g. per-employee wage
    rows) that add tokens but no field-extraction value. We keep the first few
    rows + a marker so the LLM still knows a table existed and sees totals."""
    lines = text.split("\n")
    out, run = [], []

    def flush(run):
        # a "noise run" = many consecutive lines dominated by currency/numbers
        if len(run) > 8:
            kept = run[:4]
            kept.append(f"... [{len(run) - 4} more line-items collapsed for brevity] ...")
            # keep any line that looks like a total
            kept += [r for r in run if re.search(r"total", r, re.I)]
            return kept
        return run

    for ln in lines:
        is_noise = bool(re.search(r"(\$\s?\d|\d{2,}\.\d{2}|\|\s*\d)", ln)) and len(ln) < 120
        if is_noise:
            run.append(ln)
        else:
            if run:
                out += flush(run); run = []
            out.append(ln)
    if run:
        out += flush(run)
    return "\n".join(out)


def _chunk(text: str) -> List[str]:
    """Overlapping chunks by paragraph, sized in tokens."""
    paras = re.split(r"\n\s*\n", text)
    chunks, cur, cur_tok = [], [], 0
    overlap_chars = CHUNK_OVERLAP_TOKENS * CHARS_PER_TOKEN
    for p in paras:
        ptok = estimate_tokens(p)
        if cur_tok + ptok > CHUNK_TOKENS and cur:
            chunk = "\n\n".join(cur)
            chunks.append(chunk)
            tail = chunk[-overlap_chars:]
            cur, cur_tok = [tail, p], estimate_tokens(tail) + ptok
        else:
            cur.append(p); cur_tok += ptok
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def segment_document(doc_name: str, text: str, start_index: int = 0,
                     trim: bool = True) -> List[Section]:
    """Decide how to feed one document to the LLM. We do NOT segment by regex —
    the LLM reads arbitrary structure far better than any anchor set. We only
    decide based on SIZE:

      <= MAX_SINGLE_CALL_TOKENS  -> "whole_doc": one LLM call. The model returns an
                                    array, so a doc with 1 or many projects is handled
                                    in a single call. Robust to ANY layout.
      >  MAX_SINGLE_CALL_TOKENS  -> "llm_detect": chunk by size (overlapping); the LLM
                                    detects projects in each chunk and the pipeline
                                    merges duplicates by title. Avoids lost-in-the-
                                    middle on huge docs.

    `trim_boilerplate` first collapses giant repetitive tables (e.g. per-employee
    wage rows) so token counts reflect real content, not noise.
    """
    if trim:
        text = trim_boilerplate(text)
    text = text.strip()
    tok = estimate_tokens(text)

    if tok <= MAX_SINGLE_CALL_TOKENS:
        sec = Section(doc=doc_name, index=start_index,
                      title_hint="(auto-detect projects)", text=text,
                      token_est=tok, pages=_page_span(text), strategy="whole_doc")
        return [sec]

    sec = Section(doc=doc_name, index=start_index,
                  title_hint="(auto-detect projects)", text=text,
                  token_est=tok, pages=_page_span(text),
                  needs_chunking=True, chunks=_chunk(text), strategy="llm_detect")
    return [sec]


def segment_batch(extracted: List[tuple]) -> List[Section]:
    """extracted = [(doc_name, text), ...]  ->  flat list of Sections."""
    sections, idx = [], 0
    for doc_name, text in extracted:
        secs = segment_document(doc_name, text, start_index=idx)
        idx += len(secs)
        sections += secs
    return sections


if __name__ == "__main__":
    import sys
    from extract import extract
    batch = [(p.split("/")[-1], extract(p).text) for p in sys.argv[1:]]
    secs = segment_batch(batch)
    print(f"\n{len(secs)} section(s) from {len(batch)} document(s):\n")
    for s in secs:
        flag = f"  -> CHUNKED into {len(s.chunks)}" if s.needs_chunking else ""
        print(f"[{s.index}] {s.doc} {s.pages or ''} ~{s.token_est} tok | {s.title_hint!r}{flag}")
