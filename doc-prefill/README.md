# Stage 9 — Document Upload & AI Prefill (R&D Proof of Concept)

Replaces the manual "Add Project" flow with a document-upload flow: extract text
from PDF/Word docs → LLM fills the Stage 9 fields → client reviews & edits.
Aggressive caching means identical re-submissions make **zero** LLM calls.

## Files
- `extract.py`   — extraction layer. PDF via PyMuPDF (text, reading order) + pdfplumber (tables);
                   DOCX via an order-preserving body walk. Flags scanned pages for OCR.
- `prefill.py`   — two-layer SHA-256 cache + OpenAI Structured-Outputs prefill against the
                   Stage 9 JSON schema. Falls back to a deterministic offline mock with no API key.
- `run_poc.py`   — end-to-end runner. Run twice to see the cache turn cold → warm.
- `make_samples.py` — generates the two synthetic test documents in `samples/`.

## Run
```bash
pip install pymupdf pdfplumber python-docx mammoth reportlab openai --break-system-packages
python3 make_samples.py        # creates samples/
python3 run_poc.py             # cold cache: extract + LLM (mock if no key)
python3 run_poc.py             # warm cache: 0 LLM calls

# real OpenAI:
export OPENAI_API_KEY=sk-...
python3 run_poc.py
```

## Recommended production stack
- PDF: PyMuPDF (text) + pdfplumber (tables); Tesseract / OpenAI vision only as scanned-doc fallback.
- DOCX: python-docx order-preserving walk (mammoth available for inline formatting).
- LLM: gpt-4o-2024-08-06 (or newer) with Structured Outputs + strict Stage 9 schema.
- Cache: content-hash store (Redis/DB/S3). LLM key = text + model + prompt version + schema version.

Note: PyMuPDF is AGPL — confirm license fit for a closed-source product or budget a commercial license / pdfium swap.
See `Stage9_DocUpload_AIPrefill_RnD_Report.docx` for the full write-up.
