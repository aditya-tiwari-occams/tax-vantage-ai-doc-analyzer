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
PROMPT_VERSION = "v13"     # bump to invalidate LLM cache on prompt change
SCHEMA_VERSION = "v4"      # bump to invalidate LLM cache on schema change

# ---- Stage-9 field contract (mirrors the portal form) ----
CONTRACT_TYPES = ["Fixed Price", "Time & Material", "Cost Plus", "Other",
                  "Work was not done under Contract"]

# Fields the model must provide a supporting verbatim citation for ("proof of
# work"). Kept in sync with citations.CITED_FIELDS — see that module for the
# grounding (quote -> page/section) and Markdown rendering logic.
CITED_FIELDS = [
    "project_title", "tax_year", "contract_type", "description",
    "total_man_hours", "employees_completing_research", "supplies_used",
    "solutions_alternatives_considered", "technical_challenges_uncertainties",
]

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
                    "total_man_hours": {"type": ["integer", "null"]},
                    "employees_completing_research": {"type": ["integer", "null"]},
                    "supplies_used": {"type": "string"},
                    "solutions_alternatives_considered": {"type": "string"},
                    "technical_challenges_uncertainties": {"type": "string"},
                    "confidence": {"type": "number"},
                    "citations": {
                        "type": "object",
                        "additionalProperties": False,
                        "description": (
                            "Proof-of-work: for each field below, a short VERBATIM "
                            "excerpt (<=200 chars) copied character-for-character from "
                            "the source text that supports the extracted value. null "
                            "if the field itself is null/empty or has no single "
                            "quotable source span."
                        ),
                        "properties": {f: {"type": ["string", "null"]} for f in CITED_FIELDS},
                        "required": CITED_FIELDS,
                    },
                },
                "required": [
                    "project_title", "tax_year", "contract_type", "description",
                    "total_man_hours", "employees_completing_research",
                    "supplies_used", "solutions_alternatives_considered",
                    "technical_challenges_uncertainties", "confidence", "citations",
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
    "formal study reports (Occams-style narrative PDFs), internal engineering memos, "
    "payroll detail reports, job cost history reports, project proposals, or spreadsheets. "
    "DO NOT assume any fixed layout. Read for MEANING, not keywords.\n"
    "\n"

    "━━━ RULE 1 — WHAT COUNTS AS A PROJECT (MOST CRITICAL) ━━━\n"
    "A PROJECT is a top-level Job or initiative identified by a unique Job Number or "
    "canonical job name. Create exactly ONE record per distinct project.\n"
    "\n"
    "The following are NOT projects — they are sub-components. DO NOT create separate "
    "records for them:\n"
    "- Phases or sub-tasks within a job (e.g. 'Phase: 032030 Seal Paired Sheetpile Joint', "
    "'Phase: 038090 Water Test Flood Door'). These are activities WITHIN a project.\n"
    "- Narrative sub-headings inside a project's analysis (e.g. a section titled "
    "'iii) Why This is a New or Improved Business Component' followed by a numbered "
    "heading like '1) Innovative Solutions for Flood Wall Construction...'). These "
    "headings describe analytical dimensions of the SAME project — not new projects.\n"
    "- Business component names, technique names, or process names that appear as "
    "sub-headings within sections iv, v, vi of an Occams study report. They are the "
    "R&D activities of the parent project, not separate projects.\n"
    "- Change orders or scope additions that do NOT have their own distinct Job Number "
    "(e.g. 'Job 314 - Fence Addition' with no separate job number → same as Job 314).\n"
    "\n"
    "BEFORE creating any record ask: 'Is this a distinct top-level Job Number / project "
    "not yet captured, OR is it a phase, sub-task, analytical sub-heading, or change "
    "order of an existing project?' If the latter — fold the data in, do NOT create "
    "a new record.\n"
    "\n"

    "━━━ RULE 2 — DEDUPLICATION: ONE RECORD PER JOB NUMBER ━━━\n"
    "The deduplication key is the Job Number (e.g. '314') or canonical job name when "
    "no number is present. If the same job appears:\n"
    "- Across multiple uploaded files → ONE record (merge all data)\n"
    "- Across multiple pages of the same file → ONE record\n"
    "- With minor name variations ('COYOTE FLOOD MANAGEMENT' vs 'Coyote Flood "
    "Management' vs 'COYOTE FLOOD MANAGEMENT - FENCE ADDITION') → same project unless "
    "a distinct separate Job Number is explicitly stated.\n"
    "\n"

    "━━━ RULE 3 — DOCUMENT TYPES AND HOW TO READ THEM ━━━\n"
    "\n"
    "TYPE A — NARRATIVE STUDY REPORTS (Occams-style formal R&D tax credit studies):\n"
    "These studies may use TWO different section formats — handle both:\n"
    "\n"
    "FORMAT A1 (older single-year studies):\n"
    "  i)   Basic data  ← PROJECT BOUNDARY — contains 'Project Name:', man hours, etc.\n"
    "  ii–vi) Analysis sections — sub-headings here are NOT separate projects\n"
    "\n"
    "FORMAT A2 (newer multi-year studies):\n"
    "  Project sections are numbered: 'Project 1:', 'Project 2:', etc.\n"
    "  The Job Number appears in PARENTHESES after the project name:\n"
    "    e.g. 'Project 3: Coyote Flood Management (Job 314)'\n"
    "    e.g. 'the Hudeman Slough Boat Ramp ecological restoration (Job 338)'\n"
    "  ALWAYS extract the Job Number from '(Job NNN)' and use it in the project title.\n"
    "  Canonical title format: 'Job NNN - Project Name'\n"
    "\n"
    "HARD RULE FOR OCCAMS STUDY REPORTS:\n"
    "A new project begins at 'i) Basic data' (Format A1) OR at a numbered project "
    "header with '(Job NNN)' (Format A2). Sub-headings within project analysis sections "
    "are NOT separate projects — they are analytical dimensions of the parent project.\n"
    "Numbered items like '1) Custom 3D-Fit ADA Handrail System' inside sections iii–vi "
    "are business component names being analyzed, NOT new project records.\n"
    "\n"
    "CRITICAL — WHEN YOUR TEXT HAS NO 'i) Basic data' SECTION:\n"
    "If the text you receive starts with a '=== DOCUMENT CONTEXT ===' header and the body "
    "that follows contains NO 'i) Basic data' / 'Project Name:' section, then:\n"
    "  1. You MUST use ONLY the project names listed in the document context header.\n"
    "  2. Map whatever content you find to those named projects.\n"
    "  3. NEVER invent a new project title — not from the company name, not from a "
    "sub-heading, not from any analytical section heading.\n"
    "  4. If you cannot confidently attribute content to a specific listed project, "
    "return an EMPTY list rather than creating a record with an invented title.\n"
    "The company name (e.g. 'Gordon N. Ball, Inc.') is NEVER a project title.\n"
    "\n"
    "TYPE B — FINANCIAL / PAYROLL COST REPORTS:\n"
    "Report header: 'Job: [NUMBER] [JOB NAME]'  ← THIS IS THE PROJECT IDENTITY\n"
    "Below it: 'Phase: [code] [description]' rows with employee/hours/cost lines.\n"
    "- The PROJECT = Job Number + Job Name from the report header.\n"
    "- Phases are sub-tasks. Never use phase descriptions as project titles.\n"
    "- SUM all hours across all phases → total_man_hours.\n"
    "- Count UNIQUE employees (deduplicated by name/code) → employees_completing_research.\n"
    "- Canonical title format: 'Job [NUMBER] - [Title Case Job Name]'\n"
    "\n"

    "━━━ RULE 4 — AGGREGATION WHEN MERGING ━━━\n"
    "When the same project appears in multiple places, merge fields as:\n"
    "- project_title: use canonical 'Job [N] - [Name]' if a job number is present\n"
    "- total_man_hours: SUM (never double-count the same phase twice)\n"
    "- employees_completing_research: count UNIQUE individuals across all sources\n"
    "- description / technical_challenges / solutions: use the richest / longest version, "
    "supplemented with additional detail from other sources\n"
    "- tax_year, contract_type: use the most explicit statement\n"
    "\n"

    "━━━ CORE EXTRACTION RULES ━━━\n"
    "- Use ONLY information actually present. NEVER invent figures, hours, names, or facts.\n"
    "- Do not derive man-hours or headcount from dollar amounts.\n"
    "- Map each field by meaning to the closest schema option.\n"
    "\n"

    "━━━ RULE D-2 — TOTAL MAN HOURS: LABOR ONLY ━━━\n"
    "Count ONLY human labor hours — transaction code PR (Payroll). "
    "EXCLUDE equipment hours (code EQ), vendor invoices (code AP), and accounting "
    "entries (GL, IC, JC, OH). Regular hours + Overtime hours = Total Man Hours. "
    "Do NOT count equipment machine hours as man hours even if they appear in an "
    "'hours' column. If a 'Total for job:' recap line is present, use that verified "
    "total rather than summing individual rows.\n"
    "Example: 'Total for job: 314   Regular: 5,926.50   OT: 133.00   Total: 6,059.50' "
    "→ total_man_hours = 6060 (round to nearest integer).\n"
    "\n"

    "━━━ RULE D-3 — EMPLOYEES: UNIQUE INDIVIDUALS BY CODE ━━━\n"
    "Count UNIQUE human employees only. The primary deduplication key is the Employee "
    "Code (e.g., AGUDAN, GILJES, GOMVIC). If an employee code is absent, deduplicate "
    "by full name. The same employee appearing in 30 phases = 1 person, not 30. "
    "EXCLUDE vendor/subcontractor entries (AP code), equipment entries (EQ code), and "
    "subcontractor company names. Only PR (Payroll) entries represent human employees.\n"
    "Example: 'PR AGUDAN DANIEL AGUILERA' in 15 phases → count as 1 employee.\n"
    "\n"

    "FIELD MEANINGS:\n"
    "- project_title: canonical project name. For cost reports: 'Job [N] - [Name]'. "
    "For study reports: the name from 'Project Name:' or the top-level project heading. "
    "NEVER use a phase name, business component sub-heading, or analytical section "
    "title (from sections iii–vi of a study) as the project title.\n"
    "- tax_year: 4-digit year from 'Tax Year(s):', 'Project Years:', 'Project Period', "
    "or a report date. Null if absent.\n"
    "- description: what was designed/built/researched across all phases/activities. <=1000 chars.\n"
    "- technical_challenges_uncertainties: engineering unknowns/risks from any section.\n"
    "- solutions_alternatives_considered: approaches evaluated, tested, or rejected.\n"
    "- total_man_hours: integer sum of ALL PR (labor) hours for this project. "
    "Null if only dollar amounts are given. See Rule D-2 above.\n"
    "- employees_completing_research: count of UNIQUE human employees (PR entries only). "
    "Deduplicate by employee code first, then name. Null if absent. See Rule D-3 above.\n"
    "- contract_type: 'lump sum'/'fixed fee'/'fixed price' -> 'Fixed Price'; "
    "'time & materials' -> 'Time & Material'; 'cost plus' -> 'Cost Plus'; "
    "explicitly no contract / internal work -> 'Work was not done under Contract'; "
    "unclear -> 'Other'.\n"
    "- supplies_used: tangible materials (cost type M = MATERIALS) and equipment rental "
    "costs (cost type E = EQUIPMENT) actually consumed in R&D. "
    "EXCLUDE: subcontractor costs (cost type S), labor costs (cost type L/PR), "
    "and statutory IRC definitions of 'supplies'. If dollar amounts are available "
    "provide them; otherwise describe the materials.\n"
    "- SCENARIO M (mixed language): If the document is in a language other than English, "
    "extract all fields in English. Translate project names and descriptions to English. "
    "Add a note in the description field: '[Translated from {language}]'.\n"
    "- confidence: 0-1 self-rating per project. "
    "0.9-1.0 = job clearly identified, all fields populated. "
    "0.75-0.89 = job clear, some fields inferred from context. "
    "0.6-0.74 = partial data available. "
    "Below 0.6 = ambiguous job identification — flag for human review.\n"
    "\n"

    "━━━ RULE 5 — CITATIONS (PROOF OF WORK) ━━━\n"
    "For EVERY field listed in `citations`, provide a short VERBATIM excerpt "
    "(<=200 characters) copied CHARACTER-FOR-CHARACTER from the source text that "
    "directly supports the value you extracted for that field. This lets a human "
    "reviewer verify each field against the original document.\n"
    "- COPY, do not paraphrase or summarize. Reproduce the exact wording, numbers, "
    "and punctuation as they appear in the source, including original capitalization.\n"
    "- Keep it short: the smallest contiguous span that proves the value (a line, "
    "a table row, a sentence fragment) — not an entire paragraph.\n"
    "- Do NOT prepend the field's label/heading to the quote (e.g. do NOT quote "
    "'Project Description: The team designed...' — quote only "
    "'The team designed...'), UNLESS the label and value appear on the same "
    "line in the source with no line break between them.\n"
    "- NEVER truncate a list or sentence with '...' inside the quote and call it "
    "verbatim — '...' is not in the source. If a list of names/items is too long "
    "to quote in full within 200 characters, quote only the first 2-3 items "
    "exactly as they appear, with no trailing ellipsis.\n"
    "- For total_man_hours / employees_completing_research, quote the line the "
    "number came from (e.g. 'Total for job: 314   Regular: 5,926.50   OT: 133.00   "
    "Total: 6,059.50'), not just the bare number.\n"
    "- For description / technical_challenges_uncertainties / "
    "solutions_alternatives_considered (which are your own synthesis across "
    "possibly multiple sentences), quote ONE short, complete, contiguous sentence "
    "or clause copied verbatim from the source — not the full field value, and "
    "not a blend of multiple non-adjacent sentences.\n"
    "- If a field's value is null, empty, or was inferred from general context with "
    "no single quotable span, set its citation to null. NEVER invent or "
    "approximate a quote that doesn't appear verbatim in the source text.\n"
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
        emp_cite = None
        m_cnt = re.search(r"Employees on Research\D*(\d+)", b, re.I)
        if m_cnt:
            emp_n = int(m_cnt.group(1))
            emp_cite = m_cnt.group(0)
        else:
            m_names = re.search(r"(?:Employees Completing Research|conducted by the following employees)\s*[:\-]\s*(.+?)(?=\n\s*\n|\Z)", b, re.S | re.I)
            if m_names:
                names = re.split(r"[;,]", re.sub(r"\s+", " ", m_names.group(1)))
                emp_n = len([n for n in names if len(n.strip()) > 2]) or None
                emp_cite = m_names.group(0)[:200]
        # contract type: map real wording to the enum
        low = b.lower()
        ctype_m = re.search(r"lump sum|fixed fee|fixed price|time and materials?|time & materials?|"
                            r"cost plus|not.{0,12}under.{0,4}contract", b, re.I)
        if re.search(r"lump sum|fixed fee|fixed price", low):   ctype = "Fixed Price"
        elif re.search(r"time and material|time & material", low): ctype = "Time & Material"
        elif "cost plus" in low:                                 ctype = "Cost Plus"
        elif re.search(r"not.{0,12}under.{0,4}contract", low):    ctype = "Work was not done under Contract"
        else: ctype = next((c for c in CONTRACT_TYPES if c.lower() in low), "Other")
        sup_m = re.search(r"Supplies Used\s*\|?\s*([^\n|]+)", b, re.I)
        # tax year: "Tax Year(s): 2025" / "Project Years: 2024" / "Period 2023-..."
        ty = re.search(r"(?:Tax Year|Project Years?|Project Period|Period)[^\d]{0,12}(20\d{2})", b, re.I)
        tax_year = ty.group(1) if ty else doc_year
        description = grab("Project Description|Description", "Technical|Process|Alternatives")
        alternatives = grab("Alternatives Considered", "Project|\\Z")
        uncertainty = grab("Technical Uncertainty|Technical Challenges", "Process|Alternatives")
        projects.append({
            "project_title": title,
            "tax_year": tax_year,
            "contract_type": ctype,
            "description": description,
            "total_man_hours": int(mh.group(1).replace(",", "")) if mh else None,
            "employees_completing_research": emp_n,
            "supplies_used": sup_m.group(1).strip() if sup_m else "",
            "solutions_alternatives_considered": alternatives,
            "technical_challenges_uncertainties": uncertainty,
            "confidence": 0.62,  # mock is low-confidence by design
            # Mock "proof of work": reuse the exact matched span as the citation
            # quote wherever we have one, so the grounding step downstream has
            # something real to locate on the page. None where we have nothing.
            "citations": {
                "project_title": title if title != "Untitled Project" else None,
                "tax_year": (ty.group(0) if ty else None),
                "contract_type": (ctype_m.group(0) if ctype_m else None),
                "description": (description[:200] if description else None),
                "total_man_hours": (mh.group(0) if mh else None),
                "employees_completing_research": emp_cite,
                "supplies_used": (sup_m.group(0) if sup_m else None),
                "solutions_alternatives_considered": (alternatives[:200] if alternatives else None),
                "technical_challenges_uncertainties": (uncertainty[:200] if uncertainty else None),
            },
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
