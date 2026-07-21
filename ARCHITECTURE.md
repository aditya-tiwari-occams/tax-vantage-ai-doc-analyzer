# Tax-Vantage AI Doc Analyzer — Architecture & Pipeline Documentation

## What This Project Does

When a tax professional uploads R&D study documents (PDFs/DOCXs) in the Tax-Vantage portal's **Stage 9** (Project Qualification), this system automatically reads those documents and pre-fills all the form fields — project title, description, man hours, employee count, technical challenges, contract type, four-part test result, and more — so the analyst doesn't have to type everything manually.

It handles **up to 10 files, ~30–35 pages each**, and returns structured JSON in a few seconds.

---

## Why It's Fast — The Core Idea

The speed comes from **using the right concurrency model for each type of work**:

| Stage | Work Type | Concurrency Model |
|---|---|---|
| Extract text from PDFs | CPU-bound (parsing) | `ProcessPoolExecutor` — true OS-level parallel processes |
| Call the LLM | I/O-bound (network wait) | `asyncio` — concurrent coroutines, non-blocking |
| Segment & aggregate | Cheap in-memory | Single-threaded, instant |

Mixing processes for CPU work and async for network work is the architectural heart of why this is fast.

---

## The 4-Stage Pipeline

```
Files uploaded
     │
     ▼
┌─────────────────────────────────────┐
│  Stage 1: EXTRACT (parallel)        │  ← runs in separate OS processes
│  PDF → PyMuPDF + pdfplumber         │
│  DOCX → python-docx (order walk)    │
└────────────────┬────────────────────┘
                 │  Clean markdown text per file
                 ▼
┌─────────────────────────────────────┐
│  Stage 2: SEGMENT (cheap)           │  ← single thread, fast
│  Trim boilerplate tables            │
│  Decide: whole_doc vs llm_detect    │
└────────────────┬────────────────────┘
                 │  List of Sections (+ optional chunks)
                 ▼
┌─────────────────────────────────────┐
│  Stage 3: PREFILL (async fan-out)   │  ← all LLM calls fire at once
│  asyncio.Semaphore caps at 5 concurrent
│  Cache-first (SHA-256 keyed)        │
│  Retry + exponential backoff        │
└────────────────┬────────────────────┘
                 │  Raw extracted projects per section
                 ▼
┌─────────────────────────────────────┐
│  Stage 4: AGGREGATE                 │  ← merge, deduplicate, flag review
│  Filter junk records                │
│  Merge cross-chunk duplicates       │
│  Add provenance (_source_file, pages)
└────────────────┬────────────────────┘
                 │
                 ▼
         JSON response
```

---

## Stage 1 — Extraction (`extract.py`)

**Goal:** Turn a PDF or DOCX into clean, reading-order Markdown text. This layer is intentionally "dumb" — it faithfully serializes the document. The LLM does the understanding.

### For PDFs (`extract_pdf`)

Two libraries work together:

1. **PyMuPDF (`fitz`)** — primary text extractor. Uses `get_text("text", sort=True)` which returns text blocks sorted by visual position (top→bottom, left→right). This gives correct reading order even for multi-column layouts.

2. **pdfplumber** — used ONLY to recover tables. Plain text extraction flattens tables into meaningless runs of numbers. pdfplumber detects table boundaries using coordinate analysis and returns structured rows/columns, converted to Markdown pipe tables (`| col1 | col2 |`).

3. **OCR detection** — if a page has `< 15 characters` of extractable text but has embedded images, it flags `needs_ocr = True` so the pipeline knows that page is a scanned image.

Each page gets wrapped in an HTML comment: `<!-- page 5 -->`. This is how the pipeline later knows which pages a section came from.

### For DOCX (`extract_docx`)

The key here is the **order-preserving XML walk**. The standard `python-docx` library gives you paragraphs OR tables separately, losing their interleaved order. This code walks the raw XML body directly, yielding each element (paragraph or table) in true document order. Headings are converted to `# ## ###` Markdown.

### Text Normalization (`_normalize`)

After extraction, the text is cleaned:
- Trailing whitespace stripped from every line
- Multiple consecutive blank lines collapsed to one
- Leading/trailing whitespace removed

---

## Stage 2 — Segmentation (`segment.py`)

**Goal:** Decide how to split extracted text into LLM-sized pieces. The key insight: **don't split by regex or headings** — the LLM reads arbitrary structure far better than any pattern matcher. Split purely by **token size**.

### Token Budget

```
~4 chars per token (offline heuristic for English prose)

MAX_SINGLE_CALL_TOKENS = 12,000   (~15 dense pages after boilerplate trimming)
CHUNK_TOKENS           =  6,000   (target chunk size when doc is oversized)
CHUNK_OVERLAP_TOKENS   =    250   (overlap to prevent splitting a project at a boundary)
```

### Two Strategies

**`whole_doc`** — document fits in one LLM call (≤ 12k tokens). The model receives the entire text and returns ALL projects as an array. One call handles 1 project or 10 projects equally well.

**`llm_detect`** — document is too large (> 12k tokens). The text is split into overlapping chunks of ~6k tokens. Each chunk goes to a separate LLM call. The 250-token overlap ensures a project straddling a chunk boundary appears in both chunks and gets merged in Stage 4.

### Boilerplate Trimming (`trim_boilerplate`)

Before sizing, repetitive noise is collapsed. Long runs of lines dominated by currency values (`$24.50`, `| 1234.00`) — likely per-employee wage tables — are reduced to:
- First 4 rows
- `"... [N more line-items collapsed for brevity] ..."`
- Any "total" rows

This can cut token count by 30–50% on dense financial documents, reducing both cost and latency.

---

## Stage 3 — LLM Prefill (`pipeline.py` + `prefill.py`)

**Goal:** Call GPT-4o with a strict JSON schema for each section, with caching, concurrency, and retry.

### Two-Layer Cache

Every LLM call is keyed by a **SHA-256 hash** of:
```
text content + model name + prompt version + schema version + JSON schema
```

This means:
- Re-upload the same file → **0 LLM calls**, instant response
- Change the prompt (`PROMPT_VERSION`) → cache misses correctly, fresh calls made
- Change the model → cache misses correctly, no stale results

Cache files live in `doc-prefill/cache/` as `llm_<hash>.json` — one file per unique section ever processed.

### Async Fan-out with Semaphore

```python
sem = asyncio.Semaphore(5)   # max 5 concurrent OpenAI calls at once

# All sections fire concurrently (within the semaphore limit)
per_section = await asyncio.gather(
    *[_process_section(s, sem, ...) for s in sections])
```

With 4 files → 4 sections, all 4 LLM calls fire concurrently. You wait for the slowest one, not the sum of all. With 15 sections: 5 run, then the next 5, then the next 5 — never more than 5 in-flight at once (OpenAI rate limit safety).

### Retry with Exponential Backoff

If OpenAI returns a rate-limit or transient error:
```
attempt 1 → wait 1s → retry
attempt 2 → wait 2s → retry
attempt 3 → wait 4s → retry
attempt 4 → raise error
```

### The LLM Call — Structured Outputs

Uses OpenAI's **Responses API with JSON Schema** (`strict: True`). This guarantees the model output always matches the schema exactly — no parsing errors, no hallucinated fields, no missing required keys.

The system prompt (`SYSTEM_PROMPT` in `prefill.py`) is carefully engineered to:
- NOT assume any fixed document layout (works on PDFs, memos, spreadsheets, proposals)
- Read for **MEANING**, not keywords
- Never invent figures, hours, names, or facts
- Count employee names if a list is given rather than an explicit count
- Map any contract wording to the nearest enum value
- Self-rate `confidence` per project so reviewers know what to check

### Map-Reduce for Oversized Sections

When a section uses `llm_detect` (too large for one call):

```
Section (oversized, e.g. 88 pages)
    │
    ├── Chunk 0 (p1–p15)  → LLM → {projects: [...]}  ┐
    ├── Chunk 1 (p13–p28) → LLM → {projects: [...]}  ├── merge_projects_by_title()
    └── Chunk 2 (p26–p88) → LLM → {projects: [...]}  ┘
                                                       └── unique merged projects
```

`merge_projects_by_title()` uses **fuzzy string matching** (`SequenceMatcher`, threshold 0.82) to identify the same project appearing in multiple chunks, then field-merges: longest non-empty string wins, max for numeric fields.

---

## Stage 4 — Aggregation (`pipeline.py`)

**Goal:** Flatten all per-section results, remove junk, add metadata.

### Junk Filter (`_is_real_project`)

Keeps a record only if:
- `project_title` has ≥ 3 alphabetic characters, AND
- At least one substantive field has content: description, technical challenges, solutions, supplies, man hours, or employee count

Removes stray LLM objects and table-fragment artefacts.

### Provenance Tagging

Every project gets metadata about where it came from:
```json
{
  "_source_file": "multi-project.pdf",
  "_source_pages": "p3-p47",
  "_detection": "llm_detect"
}
```

### Review Flagging (`_flag_review`)

A project is flagged `_needs_review: true` if:
- `confidence < 0.7`, OR
- Any of `description`, `technical_challenges_uncertainties`, or `solutions_alternatives_considered` is empty

---

## Fields Extracted

These mirror the Tax-Vantage Stage 9 form exactly:

| Field | Type | Source in Document |
|---|---|---|
| `project_title` | string | Headings, "Project Name:", "Job N -" |
| `tax_year` | string / null | "Tax Year: 2025", "Project Period 2024-..." |
| `contract_type` | enum (5 values) | "Contract Type:" line — mapped to nearest enum |
| `description` | string ≤ 1000 chars | "Project Description" / narrative sections |
| `technical_challenges_uncertainties` | string | "Key Technical Uncertainty", four-part test criteria |
| `solutions_alternatives_considered` | string | "Process of Experimentation", approaches tried/rejected |
| `total_man_hours` | integer / null | "Man hours: 24382" — never derived from $ amounts |
| `employees_completing_research` | integer / null | Count of distinct names, or explicit number |
| `supplies_used` | string | Materials actually consumed in research |
| `confidence` | 0.0–1.0 | Model's self-rating per project |

### Contract Type Mapping

| Document Wording | Enum Value |
|---|---|
| "lump sum", "fixed fee", "fixed price" | `Fixed Price` |
| "time and materials", "time & materials" | `Time & Material` |
| "cost plus" | `Cost Plus` |
| Explicitly no contract / internal work | `Work was not done under Contract` |
| Anything else | `Other` |

---

## The API Layer (`app.py`)

A **FastAPI** server with two endpoints:

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Serves `stage9-mockup.html` frontend |
| `/api/health` | GET | Health check + OpenAI key presence |
| `/api/prefill` | POST | Accepts 1–10 files, runs pipeline, returns JSON |

Files are written to a temp directory, processed, then the temp dir is deleted in a `finally` block — no uploaded files persist on disk.

**Limits:** 10 files max · 20 MB per file · `.pdf` / `.docx` / `.doc` only

---

## Why It's Accurate

1. **Small focused inputs** — each LLM call gets one section (~15 pages max), not a 100-page batch. No "lost in the middle" degradation.
2. **Structured outputs** — GPT-4o with `strict: True` JSON Schema guarantees no hallucinated fields and no schema violations.
3. **Prompt reads for meaning** — explicitly told not to rely on fixed headings or layouts; works on any document structure.
4. **Field contract** — `FIELD_MAPPING.md` defines exactly what each field means and where to find it, encoded into the system prompt.
5. **Merge logic** — cross-chunk project deduplication by fuzzy title match prevents double-counting.

---

## Project File Structure

```
tax-vantage-ai/
├── ARCHITECTURE.md          ← this file
├── .gitignore
│
├── stage9-mockup.html       ← frontend UI mockup for Stage 9
│
├── required-docs/           ← sample R&D study PDFs for testing
│   ├── 1-project.pdf
│   └── multi-project.pdf
│
└── doc-prefill/             ← the entire backend pipeline
    ├── app.py               ← FastAPI server (entry point)
    ├── pipeline.py          ← orchestrator: stages 1–4
    ├── extract.py           ← Stage 1: PDF/DOCX → clean text
    ├── segment.py           ← Stage 2: size-based segmentation
    ├── prefill.py           ← Stage 3: LLM schema, prompt, cache
    ├── logger.py            ← structured run logging
    ├── make_samples.py      ← generate test documents
    ├── run_poc.py           ← CLI runner
    ├── test_scale.py        ← scale/load tests
    ├── requirements.txt
    ├── .env.example
    ├── README.md
    ├── RUNBOOK.md
    └── FIELD_MAPPING.md     ← field-by-field extraction contract
```

---

## Running Locally

```bash
cd doc-prefill
pip install -r requirements.txt
cp .env.example .env          # paste your OPENAI_API_KEY
uvicorn app:app --reload --port 8001
```

Open `http://localhost:8001` — upload PDFs and watch the form pre-fill.

**No API key?** The pipeline falls back to an offline mock extractor so the full flow still runs end-to-end (with lower accuracy and `confidence: 0.62`).

---

## Telemetry

Every run returns a `telemetry` block:

```json
{
  "llm_calls": 3,
  "cache_hits": 1,
  "retries": 0,
  "sections": 4,
  "chunks": 6,
  "files": 2,
  "projects": 5,
  "needs_review": 1,
  "strategies": { "whole_doc": 2, "llm_detect": 2 },
  "elapsed_ms": 4821
}
```

Full structured logs (inputs, outputs, segmentation decisions, LLM I/O) are saved to `doc-prefill/logs/<run_id>/` for every run.
