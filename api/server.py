"""
TaxVantage AI Doc Prefill — API Server

Starts the FastAPI server. Test it with Postman or test_api.py.

Usage:
    cd api
    python server.py

Endpoints (once running):
    POST http://localhost:8001/api/prefill   ← upload files, get extracted projects
    GET  http://localhost:8001/api/health    ← server + API key status
    GET  http://localhost:8001/docs          ← Swagger UI (browser)
    GET  http://localhost:8001/redoc         ← ReDoc docs (browser)

Postman setup:
    Method: POST
    URL:    http://localhost:8001/api/prefill
    Body:   form-data
    Key:    files   (change type dropdown to File)
    Value:  select your PDF or DOCX file
    → Add multiple rows with the same key "files" for multi-file upload
"""
import sys
import os

# Add the doc-prefill directory to Python path so app.py can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "doc-prefill"))

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        reload_dirs=[os.path.join(os.path.dirname(__file__), "..", "doc-prefill")],
        log_level="info",
    )
