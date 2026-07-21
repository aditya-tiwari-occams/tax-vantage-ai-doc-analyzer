# TaxVantage AI Doc Prefill — API

This folder contains everything needed to run and test the API.

## Quick Start

### 1. Start the server

```bash
cd api
python server.py
```

Server runs at **http://localhost:8001**

---

### 2. Test with Postman

| Field | Value |
|---|---|
| Method | `POST` |
| URL | `http://localhost:8001/api/prefill` |
| Body | `form-data` |
| Key | `files` (change type to **File**) |
| Value | Select your PDF or DOCX file |

For **multiple files**, add more rows — all with the same key `files`.

**Health check:**
```
GET http://localhost:8001/api/health
```

**Swagger UI (in browser):**
```
GET http://localhost:8001/docs
```

---

### 3. Test with Python script

```bash
# Single file
python test_api.py ../required-docs/multi-project.pdf

# Multiple files
python test_api.py ../required-docs/multi-project.pdf ../required-docs/1-project.pdf

# Save raw JSON output
python test_api.py ../required-docs/multi-project.pdf --save
```

---

## Files in This Folder

| File | Purpose |
|---|---|
| `server.py` | Start the API server |
| `test_api.py` | Python test client |
| `README.md` | This file |

## API Response Shape

```json
{
  "projects": [
    {
      "project_title": "Job 314 - Coyote Flood Management",
      "tax_year": "2024",
      "contract_type": "Fixed Price",
      "description": "...",
      "total_man_hours": 24382,
      "employees_completing_research": 85,
      "supplies_used": "...",
      "solutions_alternatives_considered": "...",
      "technical_challenges_uncertainties": "...",
      "confidence": 0.95,
      "_needs_review": false,
      "_review_reasons": []
    }
  ],
  "telemetry": {
    "llm_calls": 3,
    "cache_hits": 0,
    "elapsed_ms": 14500,
    "files": 1,
    "projects": 2
  },
  "warnings": []
}
```

## Environment

Set your OpenAI API key in `doc-prefill/.env`:

```
OPENAI_API_KEY=sk-...
```

Without it, the pipeline runs in **offline mock mode** (low confidence, placeholder data).
