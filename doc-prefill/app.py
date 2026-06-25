"""
FastAPI server for the Stage-9 document-upload prefill demo.

It does two things:
  1. Serves the HTML mockup at  GET  /
  2. Runs the extraction + AI-prefill pipeline at  POST /api/prefill

The OpenAI key is read from a .env file (see .env.example) — no `export` needed.
If no key is present the pipeline falls back to the offline mock, so the whole
demo still runs.

Run it:
    pip install -r requirements.txt
    cp .env.example .env        # then paste your OPENAI_API_KEY into .env
    uvicorn app:app --reload --port 8001
Then open  http://localhost:8001  in your browser.
"""
from __future__ import annotations
import os, tempfile, shutil, traceback
from typing import List

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse

from pipeline import run_batch          # this import also triggers .env loading

HERE = os.path.dirname(os.path.abspath(__file__))
MOCKUP = os.path.join(HERE, "..", "stage9-mockup.html")

ALLOWED = {".pdf", ".docx", ".doc"}
MAX_FILES = 10
MAX_BYTES = 20 * 1024 * 1024          # 20 MB per file

app = FastAPI(title="TaxVantage Stage-9 Prefill")


@app.get("/", response_class=HTMLResponse)
def index():
    """Serve the Stage-9 mockup page."""
    with open(MOCKUP, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/health")
def health():
    return {"ok": True, "openai_key_present": bool(os.environ.get("OPENAI_API_KEY"))}


@app.post("/api/prefill")
async def prefill(files: List[UploadFile] = File(...)):
    """Accept 1–10 PDF/DOCX files, run the pipeline, return prefilled projects."""
    if not files:
        return JSONResponse({"error": "No files uploaded."}, status_code=400)
    if len(files) > MAX_FILES:
        return JSONResponse({"error": f"Too many files (max {MAX_FILES})."}, status_code=400)

    tmp = tempfile.mkdtemp(prefix="upload_")
    paths = []
    try:
        for f in files:
            ext = os.path.splitext(f.filename or "")[1].lower()
            if ext not in ALLOWED:
                return JSONResponse(
                    {"error": f"Unsupported file type: {f.filename}"}, status_code=400)
            data = await f.read()
            if len(data) > MAX_BYTES:
                return JSONResponse(
                    {"error": f"File too large: {f.filename}"}, status_code=400)
            p = os.path.join(tmp, os.path.basename(f.filename))
            with open(p, "wb") as out:
                out.write(data)
            paths.append(p)

        result = await run_batch(paths)
        return JSONResponse(result)
    except Exception as e:                       # don't leak stack traces to UI
        traceback.print_exc()
        return JSONResponse({"error": f"Processing failed: {e}"}, status_code=500)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=True)
