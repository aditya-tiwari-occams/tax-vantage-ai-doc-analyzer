"""
TaxVantage AI Doc Prefill — Production API Server with Auth + CORS

Adds on top of the base app:
  - Optional API key authentication via X-API-Key header
  - CORS headers so any frontend/Postman can call it
  - Request ID tracking for each call
  - Structured error responses

Setup:
    1. Set API_KEY in doc-prefill/.env (or leave blank to disable auth)
       API_KEY=your-secret-key-here

    2. Start this server:
       cd api
       python api_server.py

Test in Postman:
    POST http://localhost:8001/api/prefill
    Headers:
        X-API-Key: your-secret-key-here    ← only if API_KEY is set in .env
        Accept: application/json
    Body: form-data
        files  (File)  →  select PDF or DOCX

    GET  http://localhost:8001/api/health
    GET  http://localhost:8001/docs        ← Swagger UI
"""
from __future__ import annotations
import sys
import os
import uuid
import time

# Add doc-prefill to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "doc-prefill"))

import hashlib
import tempfile
import shutil
import traceback
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security.api_key import APIKeyHeader

from pipeline import run_batch

# ── Load env vars ──────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(__file__), "..", "doc-prefill", ".env")
    load_dotenv(env_path)
except Exception:
    pass

ALLOWED   = {".pdf", ".docx", ".doc"}
MAX_FILES = 10
MAX_BYTES = 20 * 1024 * 1024   # 20 MB per file
API_KEY   = os.environ.get("API_KEY", "")  # blank = auth disabled

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(
    title="TaxVantage AI — R&D Doc Prefill API",
    description=(
        "Upload R&D study documents (PDF/DOCX) and receive auto-extracted "
        "Stage-9 project qualification fields as structured JSON."
    ),
    version="1.0.0",
    contact={"name": "Occams Advisory"},
)

# ── CORS — allow any origin (tighten for production) ──────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ── API Key auth ───────────────────────────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(key: Optional[str] = Depends(api_key_header)):
    """Validate X-API-Key header. Auth is disabled when API_KEY env var is blank."""
    if not API_KEY:
        return          # auth disabled — allow all requests
    if key != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Pass it as the X-API-Key header.",
        )

# ── Request timing middleware ──────────────────────────────────────────────────
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    start = time.time()
    response = await call_next(request)
    elapsed = int((time.time() - start) * 1000)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time"] = f"{elapsed}ms"
    return response


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/api/health", tags=["Status"])
def health():
    """
    Health check. Returns server status and whether the OpenAI key is configured.

    **Postman:** GET http://localhost:8001/api/health
    """
    return {
        "ok": True,
        "openai_key_present": bool(os.environ.get("OPENAI_API_KEY")),
        "auth_enabled": bool(API_KEY),
        "max_files": MAX_FILES,
        "max_file_size_mb": MAX_BYTES // (1024 * 1024),
        "accepted_formats": sorted(ALLOWED),
    }


@app.post(
    "/api/prefill",
    tags=["Extraction"],
    dependencies=[Depends(verify_api_key)],
    summary="Upload documents and extract R&D project qualification fields",
    response_description="Extracted projects with telemetry and warnings",
)
async def prefill(files: List[UploadFile] = File(..., description="PDF or DOCX files (1–10)")):
    """
    Upload 1–10 PDF or DOCX files. The pipeline will:
    1. Extract text from all files in parallel
    2. Segment into per-project sections
    3. Call GPT-4o with a structured JSON schema per section
    4. Deduplicate, merge, and filter results

    **Postman setup:**
    - Method: POST
    - URL: http://localhost:8001/api/prefill
    - Headers: X-API-Key: your-key (if API_KEY is set)
    - Body: form-data
        - Key: `files`  Type: File  Value: select PDF/DOCX
        - (repeat key `files` for each additional file)

    **Returns:** JSON with `projects`, `telemetry`, and `warnings`.
    """
    if not files:
        return JSONResponse({"error": "No files uploaded."}, status_code=400)
    if len(files) > MAX_FILES:
        return JSONResponse({"error": f"Too many files (max {MAX_FILES})."}, status_code=400)

    tmp = tempfile.mkdtemp(prefix="upload_")
    paths: list[str] = []
    warnings: list[str] = []

    try:
        seen_hashes: dict[str, str] = {}

        for f in files:
            ext = os.path.splitext(f.filename or "")[1].lower()
            if ext not in ALLOWED:
                return JSONResponse(
                    {"error": f"Unsupported file type: '{f.filename}'. "
                              f"Accepted: {', '.join(sorted(ALLOWED))}"},
                    status_code=400,
                )

            data = await f.read()

            if len(data) > MAX_BYTES:
                return JSONResponse(
                    {"error": f"File too large: '{f.filename}' "
                              f"({len(data) // 1024} KB > {MAX_BYTES // 1024} KB limit)."},
                    status_code=413,
                )

            if len(data) == 0:
                warnings.append(f"File '{f.filename}' is empty and was skipped.")
                continue

            file_hash = hashlib.sha256(data).hexdigest()
            if file_hash in seen_hashes:
                warnings.append(
                    f"Duplicate file skipped: '{f.filename}' is identical to "
                    f"'{seen_hashes[file_hash]}'.")
                continue
            seen_hashes[file_hash] = f.filename or "unknown"

            p = os.path.join(tmp, os.path.basename(f.filename))
            with open(p, "wb") as out:
                out.write(data)
            paths.append(p)

        if not paths:
            return JSONResponse({
                "error": "No processable files after deduplication/validation.",
                "warnings": warnings,
            }, status_code=400)

        result = await run_batch(paths)

        if warnings:
            result.setdefault("warnings", []).extend(warnings)

        if not result.get("projects"):
            result["message"] = (
                "No R&D project data was found in the uploaded files. "
                "Please upload project cost reports, payroll records, "
                "or R&D study documents.")
            scanned = [n for n, m in result.get("extraction", {}).items()
                       if m.get("needs_ocr")]
            if scanned:
                result["message"] += (
                    f" Scanned/image-only files detected (no text layer): "
                    + ", ".join(f"'{s}'" for s in scanned)
                    + ". Please upload text-selectable PDF versions.")

        return JSONResponse(result)

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            {"error": "Processing failed.", "detail": str(e)},
            status_code=500,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    auth_msg = f"API key auth ENABLED (set X-API-Key: {API_KEY[:4]}...)" if API_KEY else "API key auth DISABLED"
    print(f"\n{'='*55}")
    print(f"  TaxVantage AI Doc Prefill API")
    print(f"  http://localhost:8001")
    print(f"  Swagger docs: http://localhost:8001/docs")
    print(f"  {auth_msg}")
    print(f"{'='*55}\n")

    uvicorn.run(
        "api_server:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        reload_dirs=[
            os.path.dirname(__file__),
            os.path.join(os.path.dirname(__file__), "..", "doc-prefill"),
        ],
        log_level="info",
    )
