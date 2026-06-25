# Stage 9 Doc-Prefill — How to Run & Test

A Python pipeline that turns uploaded PDFs/Word docs into prefilled Stage-9
project fields. Built to scale to 10 files × ~30 pages each.

## 0. Install
```bash
pip install pymupdf pdfplumber python-docx mammoth reportlab openai --break-system-packages
```

## 1. Files
| File | Role |
|------|------|
| `extract.py`   | PDF (PyMuPDF + pdfplumber tables) and DOCX (order-preserving walk) → clean Markdown |
| `segment.py`   | Splits each doc into **per-project sections**; trims boilerplate; chunks oversized sections; token estimates |
| `prefill.py`   | JSON schema, system prompt, SHA-256 cache, single LLM call (real or offline mock) |
| `pipeline.py`  | **Async fan-out**: process-pool extraction + bounded-concurrency LLM per section + map-reduce + aggregation |
| `run_poc.py`   | Simple single/two-file demo with cache stats |
| `test_scale.py`| Simulates 10 files; proves concurrency speedup + caching |

## 2. Quick test — does extraction work?
```bash
python3 extract.py ../required-docs/1-project.pdf        # prints clean markdown + stats
python3 segment.py ../required-docs/*.pdf                # shows per-project sections + token sizes
```
Expect: the 17-page study → 1 section (chunked); the multi-project study → Job 314 + Job 317.

## 3. Run the full pipeline (offline mock)
```bash
python3 pipeline.py ../required-docs/1-project.pdf ../required-docs/multi-project.pdf
```
Prints each section as it completes, the aggregated projects with provenance
(source file + page span), and telemetry (llm_calls, cache_hits, retries, elapsed).

## 4. Prove it scales — 10 files, concurrency + caching
```bash
python3 test_scale.py
```
Example result (offline, simulated 0.30s/call latency):
```
10 PDFs x 3 projects = 30 projects (small files -> 1 call each, model segments)
concurrency= 1  ->  30 projects | 10 llm_calls |  3.2s
concurrency= 8  ->  30 projects | 10 llm_calls |  0.8s     (~4x faster)
warm re-run     ->  30 projects |  0 llm_calls |  0.2s     (all cache hits)
```

## 5. Run the WEB APP (open the site + backend together)
This is the real end-to-end demo: a browser page that uploads documents to a
backend which runs the pipeline.

```bash
pip install -r requirements.txt
cp .env.example .env          # then paste your OPENAI_API_KEY into .env
uvicorn app:app --reload --port 8000
```
Open **http://localhost:8000** in your browser. Click **Upload Project Documents**,
choose PDFs/DOCX, and **Extract & Prefill** → the page fills the Stage-9 cards
from the backend.

- The key is read from `.env` automatically — **no `export` needed**.
- `GET  /`            serves the page.
- `POST /api/prefill` runs the pipeline (multipart file upload).
- `GET  /api/health`  shows whether the OpenAI key was found.
- No key in `.env`? It still runs using the offline mock (plumbing-only quality).

### CLI alternative (no web)
```bash
python3 pipeline.py ../required-docs/*.pdf     # .env is loaded automatically
```

## 5b. Verifying each step — terminal logs + logs/ folder
Every run (CLI or web) prints step-by-step progress to the terminal AND writes a
full audit folder you can open afterwards.

Terminal shows: files received → extraction (pages/chars/OCR) → segmentation
(strategy + token sizes) → each LLM call (size, cache hit/miss) → final summary
(projects, review flags, calls, cache hits, ms), including each project's tax_year.

On disk — `logs/<run_id>/`:
```
run.log                      human-readable step log
01_extracted/<file>.txt      raw extracted text per uploaded file
02_segmentation.json         sections, strategy, token sizes, chunk counts
03_llm/<n>_input.txt         exact text sent to the model for each call
03_llm/<n>_output.json       exact JSON the model returned
04_result.json               final aggregated projects + telemetry
```
The API also returns `telemetry.run_id` and `telemetry.log_dir` so you can jump
straight to the right folder. (`logs/` is git-ignored.)

## 5c. Tax year
Each project's `tax_year` is extracted from the document (e.g. "Tax Year(s): 2025",
"Project Years: 2024", "Project Period 2023-…"). The page header **"Add projects
for years: …"** updates to the years actually found, and each card shows a `TY <year>`
badge — so a 2024 study shows 2024, a 2025 study shows 2025.

## 6. Tunables (env vars)
| Var | Default | Meaning |
|-----|---------|---------|
| `PREFILL_CONCURRENCY` | 5 | Max simultaneous OpenAI calls (raise/lower to fit your rate limit) |
| `PREFILL_MAX_RETRIES` | 4 | Retries with exponential backoff on rate-limit/transient errors |
| `PREFILL_MODEL` | gpt-4o-2024-08-06 | Model used for prefill |
| `POC_CACHE` | `./cache` | Cache directory |
| `PREFILL_MOCK_LATENCY` | 0.05 | Simulated per-call latency for offline tests |

Bump `PROMPT_VERSION` / `SCHEMA_VERSION` in `prefill.py` when you change the
prompt or schema — the LLM cache will correctly invalidate.

## How the scale problem is handled (summary)
The system is **format-agnostic**: the LLM does the project segmentation (it reads
any layout), so there is NO reliance on brittle heading regexes. Size — not
structure — decides how a document is fed to the model.

1. **Right-sized LLM calls.**
   - A document up to ~12k tokens (after boilerplate trim) → **one call**
     (`whole_doc`). The model returns the full project array, so a doc with 1 or
     many projects is handled in a single call. This is robust to any format.
   - A larger document → **`llm_detect`**: chunk by size with overlap, detect
     projects in each chunk, then **merge duplicates by fuzzy title**. Avoids
     "lost in the middle" on huge docs.
2. **Right concurrency per stage.** Extraction is CPU-bound → process pool across
   files. LLM calls are I/O-bound → asyncio with a semaphore capping concurrent
   requests, plus retry/backoff for rate limits.
3. **Boilerplate trimming** collapses giant repetitive tables (e.g. per-employee
   wage rows) before the model sees them — real content, fewer tokens.
4. **Quality guards.** Empty/junk records are dropped; projects with low confidence
   or missing key fields are flagged `_needs_review` for the UI.
5. **Caching** at file-hash (extraction) and text-hash (LLM) means repeats and
   boilerplate cost nothing, and re-uploads are instant.
```
extract (processes) → size check → whole_doc: 1 call  ───┐
                                  → llm_detect: chunk → detect → merge-by-title
   (async, capped by semaphore)        ↑ cache: text-hash      ↓
                                                    drop junk → flag review → aggregate
```
Tested across 4 very different shapes: the two Occams studies, a differently-worded
"Initiative 1/2" memo, and an unstructured prose memo — all handled without any
format-specific code.
