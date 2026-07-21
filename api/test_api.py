"""
TaxVantage AI Doc Prefill — Python API Test Client

Usage:
    python test_api.py path/to/doc1.pdf [path/to/doc2.pdf ...]
    python test_api.py path/to/doc1.pdf --save     # also saves raw JSON

Requirements:
    pip install requests

The server must be running first:
    python server.py
"""
from __future__ import annotations
import sys
import json
import requests

API_URL  = "http://localhost:8001/api/prefill"
HEALTH_URL = "http://localhost:8001/api/health"


def check_health() -> bool:
    try:
        r = requests.get(HEALTH_URL, timeout=5)
        r.raise_for_status()
        data = r.json()
        key_status = "present ✅" if data.get("openai_key_present") else "missing ⚠  (mock mode)"
        print(f"Server: OK  |  OpenAI key: {key_status}\n")
        return True
    except requests.ConnectionError:
        print("❌  Cannot reach server at http://localhost:8001")
        print("    Make sure it is running: python server.py")
        return False


def prefill(file_paths: list[str]) -> dict:
    files = [
        ("files", (p.split("/")[-1], open(p, "rb"), "application/octet-stream"))
        for p in file_paths
    ]
    resp = requests.post(API_URL, files=files, timeout=300)
    resp.raise_for_status()
    return resp.json()


def print_result(result: dict) -> None:
    for w in result.get("warnings", []):
        print(f"⚠  {w}")

    if result.get("message"):
        print(f"\n💬  {result['message']}\n")

    projects = result.get("projects", [])
    if not projects:
        print("No projects extracted.")
        return

    print(f"{'─'*65}")
    print(f"  {len(projects)} project(s) extracted")
    print(f"{'─'*65}\n")

    for i, p in enumerate(projects, 1):
        review  = "  ⚠ REVIEW" if p.get("_needs_review") else ""
        conf    = f"{p.get('confidence', 0) * 100:.0f}%"
        print(f"[{i}]  {p.get('project_title', '?')}  |  TY:{p.get('tax_year','?')}  |  conf:{conf}{review}")
        print(f"     Contract:   {p.get('contract_type','?')}")
        print(f"     Man hours:  {p.get('total_man_hours') or '—'}  |  "
              f"Employees: {p.get('employees_completing_research') or '—'}")
        desc = p.get("description") or ""
        if desc and desc != "[Not found in uploaded documents]":
            print(f"     Desc: {desc[:140]}{'...' if len(desc) > 140 else ''}")
        if p.get("_review_reasons"):
            print(f"     Review reasons: {', '.join(p['_review_reasons'])}")
        print()

    t = result.get("telemetry", {})
    print(f"{'─'*65}")
    print(f"  ⏱  {t.get('elapsed_ms','?')} ms  |  "
          f"{t.get('llm_calls',0)} LLM call(s)  |  "
          f"{t.get('cache_hits',0)} cache hit(s)  |  "
          f"{t.get('files',0)} file(s)")
    print(f"{'─'*65}\n")


if __name__ == "__main__":
    args   = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags  = [a for a in sys.argv[1:] if a.startswith("--")]

    if not args:
        print(__doc__)
        sys.exit(1)

    if not check_health():
        sys.exit(1)

    print(f"Uploading {len(args)} file(s):")
    for p in args:
        print(f"  • {p.split('/')[-1]}")
    print()

    try:
        result = prefill(args)
    except requests.HTTPError as e:
        print(f"❌  API error {e.response.status_code}: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"❌  {e}")
        sys.exit(1)

    print_result(result)

    if "--save" in flags:
        out = "prefill_result.json"
        with open(out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Raw JSON saved to: {out}")
