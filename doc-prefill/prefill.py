"""
Caching + LLM prefill for the Stage-9 document-upload feature.

Pipeline:  upload bytes -> sha256 -> [extraction cache] -> extract text
                                  -> sha256(text + schema + prompt + model)
                                     -> [LLM cache] -> OpenAI structured output
                                  -> list[Project] mapped to Stage-9 fields

Two cache layers, both keyed by a content hash, so:
  * Re-uploading the SAME file        -> 0 extraction, 0 LLM calls.
  * A different file with identical    -> still 0 LLM calls if extracted text +
    extracted text                        prompt + model + schema are unchanged.

The LLM cache key intentionally includes the model name, prompt version and
schema version: change any of those and the cache correctly misses so you don't
serve stale results after a prompt/model upgrade.

If OPENAI_API_KEY is set the real Responses API is used (gpt-4o-2024-08-06 with
Structured Outputs). Otherwise a deterministic MOCK extractor runs so the PoC is
fully runnable offline and in CI.
"""
from __future__ import annotations
import os, json, hashlib, time, re
from dataclasses import dataclass, asdict
from typing import List, Optional

from extract import extract, ExtractionResult

# Load configuration from a .env file (OPENAI_API_KEY, tunables) if present.
# This must run BEFORE we read os.environ below. No-op if python-dotenv isn't
# installed or there's no .env file.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    load_dotenv()  # also check the current working directory
except Exception:
    pass

CACHE_DIR = os.environ.get("POC_CACHE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache"))
os.makedirs(CACHE_DIR, exist_ok=True)

MODEL = os.environ.get("PREFILL_MODEL", "gpt-4o-2024-08-06")
PROMPT_VERSION = "v4"      # bump to invalidate LLM cache on prompt change
SCHEMA_VERSION = "v2"      # bump to invalidate LLM cache on schema change

# ---- Stage-9 field contract (mirrors the portal form) ----
CONTRACT_TYPES = ["Fixed Price", "Time & Material", "Cost Plus", "Other",
                  "Work was not done under Contract"]

PROJECT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "projects": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project_title": {"type": "string"},
                    "tax_year": {"type": ["string", "null"]},
                    "contract_type": {"type": "string", "enum": CONTRACT_TYPES},
                    "description": {"type": "string"},
                    "qualification_status": {"type": "string", "enum": ["Qualified", "Not Qualified"]},
                    "funded": {"type": "string", "enum": ["Yes", "No"]},
                    "passes_four_part_test": {"type": "string", "enum": ["Yes", "No"]},
                    "total_man_hours": {"type": ["integer", "null"]},
                    "employees_completing_research": {"type": ["integer", "null"]},
                    "supplies_used": {"type": "string"},
                    "solutions_alternatives_considered": {"type": "string"},
                    "technical_challenges_uncertainties": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "project_title", "tax_year", "contract_type", "description",
                    "qualification_status", "funded", "passes_four_part_test",
                    "total_man_hours", "employees_completing_research",
                    "supplies_used", "solutions_alternatives_considered",
                    "technical_challenges_uncertainties", "confidence",
                ],
            },
        }
    },
    "required": ["projects"],
}

SYSTEM_PROMPT = (
    "You are an R&D tax-credit analyst extracting Stage-9 project-qualification "
    "fields from a company's R&D documentation.\n"
    "\n"
    "IMPORTANT: documents vary WIDELY in structure, format and wording. They may be "
    "formal study reports, internal engineering memos, project proposals, narrative "
    "write-ups, slide text, or spreadsheets exported to text. DO NOT assume any "
    "fixed layout, section names, or headings. Read for MEANING, not keywords.\n"
    "\n"
    "Identify each DISTINCT R&D project / business component described and return "
    "one record per project. A document may contain one project or many. If the same "
    "project is described in multiple places, merge it into a single record.\n"
    "\n"
    "CORE RULES:\n"
    "- Use ONLY information actually present. If a value is absent, leave strings "
    "empty (\"\") and numbers null. NEVER invent figures, hours, names, or facts.\n"
    "- Map each field by its MEANING to the closest schema option, regardless of how "
    "the source phrases it.\n"
    "- Do not derive man-hours or headcount from dollar amounts.\n"
    "\n"
    "FIELD MEANINGS (the example phrasings are illustrative, NOT exhaustive — expect "
    "other wordings):\n"
    "- project_title: the project's name/identifier (e.g. 'Project Name: ...', "
    "'Job 314 - ...', 'Initiative', a heading, or inferred from context).\n"
    "- tax_year: the tax year the project's R&D was performed/claimed, as a 4-digit "
    "string (e.g. 'Tax Year(s): 2025' -> '2025'; 'Project Years: 2024' -> '2024'; "
    "'Project Period 2023-01-01 ...' -> '2023'). If multiple years, use the primary/"
    "earliest claimed year. Null if no year is stated.\n"
    "- description: what was designed/built/researched. Summarize concisely (<=1000 chars).\n"
    "- technical_challenges_uncertainties: the technical unknowns/risks (e.g. "
    "'technical uncertainty', a four-part-test uncertainty criterion, 'challenges').\n"
    "- solutions_alternatives_considered: approaches evaluated/tried/rejected, "
    "experimentation, options weighed.\n"
    "- total_man_hours: labor hours as an integer if stated anywhere (e.g. 'Man "
    "hours: 24382', 'labor hours', 'total hours'); null if only dollars are given.\n"
    "- employees_completing_research: a COUNT. If a number is given, use it; if a "
    "list of people's names is given, count the distinct names; null if neither.\n"
    "- contract_type: closest of the enum. Examples: 'lump sum'/'fixed fee'/'fixed "
    "price' -> 'Fixed Price'; 'time & materials' -> 'Time & Material'; 'cost plus' -> "
    "'Cost Plus'; other contract types -> 'Other'; explicitly no contract / internal "
    "work -> 'Work was not done under Contract'. If unclear, use 'Other'.\n"
    "- supplies_used: materials/supplies actually consumed in the research. Ignore "
    "boilerplate statutory definitions of the word 'supplies'.\n"
    "- funded: 'No' if the taxpayer bore the financial risk / the funded-research "
    "exclusion does not apply; 'Yes' only if the work was paid for by another party "
    "with risk shifted. If genuinely unclear, default 'No' and lower confidence.\n"
    "- passes_four_part_test: 'Yes' if the document indicates the project meets the "
    "IRC §41 four-part test (or all four tests pass); else 'No'.\n"
    "- qualification_status: 'Qualified' if treated as qualified research; else "
    "'Not Qualified'.\n"
    "- confidence: your 0-1 confidence for THIS project. Lower it substantially when "
    "the document is ambiguous or key fields had to be left empty, so reviewers know "
    "what to check."
)


@dataclass
class CacheStats:
    extract_hit: bool = False
    llm_hit: bool = False
    llm_called: bool = False
    elapsed_ms: int = 0


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_text(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8")); h.update(b"\x00")
    return h.hexdigest()


def _cache_get(key: str) -> Optional[dict]:
    p = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


def _cache_put(key: str, value: dict) -> None:
    p = os.path.join(CACHE_DIR, key + ".json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(value, f, indent=2)
    os.replace(tmp, p)


# ---------------- LLM call (real or mock) ----------------

def _call_openai(text: str) -> dict:
    from openai import OpenAI
    client = OpenAI()
    resp = client.responses.create(
        model=MODEL,
        input=[{"role": "system", "content": SYSTEM_PROMPT},
               {"role": "user", "content": text}],
        text={"format": {"type": "json_schema", "name": "stage9_projects",
                          "schema": PROJECT_JSON_SCHEMA, "strict": True}},
    )
    return json.loads(resp.output_text)


def _mock_llm(text: str) -> dict:
    """Deterministic, offline stand-in. Splits on project headings and pulls
    fields with simple regexes so the PoC runs end-to-end without an API key.
    The REAL model is far more robust; this only proves the plumbing + caching."""
    # Segment on the headings seen across BOTH synthetic samples and the real
    # Occams study reports ("Project:" / "Project N:" / "Business Component:").
    blocks = re.split(r"(?:^|\n)#{0,3}\s*(?:Project\s+(?:\d+\s*[:\-]|[:\-])|Project Name\s*[:\-]|Business Component\s*[:\-])\s*", text)
    blocks = [b for b in blocks if b.strip() and
              re.search(r"Uncertain|Description|Problem|Component|Experiment", b, re.I)]
    if not blocks and text.strip():
        # llm_detect / anchorless path: the real model reads prose; the mock just
        # treats the chunk as a single project so the pipeline plumbing runs.
        blocks = [text.strip()]
    # document-level tax-year fallback (often only on the cover page)
    _dy = re.search(r"(?:Tax Year|Project Years?|Project Period|Period)[^\d]{0,12}(20\d{2})", text, re.I)
    doc_year = _dy.group(1) if _dy else None
    projects = []
    for b in blocks:
        def grab(label, nxt):
            m = re.search(rf"(?:{label})\s*\n+(.*?)(?=\n+(?:{nxt})|\Z)", b, re.S | re.I)
            return re.sub(r"\s+", " ", m.group(1)).strip() if (m and m.group(1)) else ""
        # title = first real line that is not a page-marker / blank
        title = next((ln.strip() for ln in b.strip().splitlines()
                      if ln.strip() and not ln.strip().startswith("<!--")), "Untitled Project")
        # man hours: "Man hours: 24382" or "Total Man Hours 4200"
        mh = re.search(r"Man[ -]?hours?\D{0,6}(\d[\d,]*)", b, re.I)
        # employees: an explicit count, OR a NAME LIST after "Employees Completing
        # Research:" / "conducted by the following employees:" -> count the names.
        emp_n = None
        m_cnt = re.search(r"Employees on Research\D*(\d+)", b, re.I)
        if m_cnt:
            emp_n = int(m_cnt.group(1))
        else:
            m_names = re.search(r"(?:Employees Completing Research|conducted by the following employees)\s*[:\-]\s*(.+?)(?=\n\s*\n|\Z)", b, re.S | re.I)
            if m_names:
                names = re.split(r"[;,]", re.sub(r"\s+", " ", m_names.group(1)))
                emp_n = len([n for n in names if len(n.strip()) > 2]) or None
        # contract type: map real wording to the enum
        low = b.lower()
        if re.search(r"lump sum|fixed fee|fixed price", low):   ctype = "Fixed Price"
        elif re.search(r"time and material|time & material", low): ctype = "Time & Material"
        elif "cost plus" in low:                                 ctype = "Cost Plus"
        elif re.search(r"not.{0,12}under.{0,4}contract", low):    ctype = "Work was not done under Contract"
        else: ctype = next((c for c in CONTRACT_TYPES if c.lower() in low), "Other")
        sup_m = re.search(r"Supplies Used\s*\|?\s*([^\n|]+)", b, re.I)
        # tax year: "Tax Year(s): 2025" / "Project Years: 2024" / "Period 2023-..."
        ty = re.search(r"(?:Tax Year|Project Years?|Project Period|Period)[^\d]{0,12}(20\d{2})", b, re.I)
        tax_year = ty.group(1) if ty else doc_year
        projects.append({
            "project_title": title,
            "tax_year": tax_year,
            "contract_type": ctype,
            "description": grab("Project Description|Description", "Technical|Process|Alternatives"),
            "qualification_status": "Qualified",
            "funded": "No",
            "passes_four_part_test": "Yes",
            "total_man_hours": int(mh.group(1).replace(",", "")) if mh else None,
            "employees_completing_research": emp_n,
            "supplies_used": sup_m.group(1).strip() if sup_m else "",
            "solutions_alternatives_considered": grab("Alternatives Considered", "Project|\\Z"),
            "technical_challenges_uncertainties": grab("Technical Uncertainty|Technical Challenges", "Process|Alternatives"),
            "confidence": 0.62,  # mock is low-confidence by design
        })
    return {"projects": projects}


def prefill_from_file(path: str) -> tuple[dict, ExtractionResult, CacheStats]:
    t0 = time.time()
    stats = CacheStats()
    with open(path, "rb") as f:
        raw = f.read()
    file_hash = _sha256_bytes(raw)

    # ---- Layer 1: extraction cache (keyed by file bytes) ----
    ex_key = "extract_" + file_hash
    cached_ex = _cache_get(ex_key)
    if cached_ex:
        stats.extract_hit = True
        ex = ExtractionResult(**cached_ex)
    else:
        ex = extract(path)
        _cache_put(ex_key, asdict(ex))

    # ---- Layer 2: LLM cache (keyed by text + model + prompt + schema) ----
    llm_key = "llm_" + _sha256_text(ex.text, MODEL, PROMPT_VERSION, SCHEMA_VERSION,
                                    json.dumps(PROJECT_JSON_SCHEMA, sort_keys=True))
    cached_llm = _cache_get(llm_key)
    if cached_llm:
        stats.llm_hit = True
        result = cached_llm
    else:
        stats.llm_called = True
        if os.environ.get("OPENAI_API_KEY"):
            result = _call_openai(ex.text)
        else:
            result = _mock_llm(ex.text)
        _cache_put(llm_key, result)

    stats.elapsed_ms = int((time.time() - t0) * 1000)
    return result, ex, stats
