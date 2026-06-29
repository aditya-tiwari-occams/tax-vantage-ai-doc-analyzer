"""
Async fan-out pipeline for the Stage-9 document-upload prefill feature.

Handles the real workload: up to 10 files, ~30-35 pages each.

Stages
------
1. EXTRACT  (CPU-bound) -> ProcessPoolExecutor across files, in parallel.
2. SEGMENT  (cheap)     -> split each doc into per-project sections (segment.py).
3. PREFILL  (I/O-bound) -> one LLM call PER SECTION, run with bounded async
                           concurrency (asyncio.Semaphore) + retry/backoff.
                           Oversized sections are chunked and map-reduced.
4. AGGREGATE            -> flatten to projects with provenance (file + pages),
                           dedup-cached at the section level.

Why this shape
--------------
- Per-section (not per-batch, not per-file) calls keep each LLM input small:
  better accuracy (no "lost in the middle") and lower cost.
- Extraction is CPU work -> processes. LLM calls are network waits -> asyncio.
  Mixing the right concurrency model for each stage is the whole point.
- A semaphore caps concurrent OpenAI calls so we stay under rate limits while
  still running ~N calls at once instead of serially.

Run offline (mock) or with OPENAI_API_KEY set (real AsyncOpenAI). See __main__.
"""
from __future__ import annotations
import os, re, json, time, asyncio, hashlib
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, asdict
from typing import List, Callable, Optional

from extract import extract
from segment import segment_batch, Section
from prefill import (PROJECT_JSON_SCHEMA, SYSTEM_PROMPT, MODEL,
                     PROMPT_VERSION, SCHEMA_VERSION, _sha256_text,
                     _cache_get, _cache_put, _mock_llm)

MAX_CONCURRENCY = int(os.environ.get("PREFILL_CONCURRENCY", "5"))
MAX_RETRIES = int(os.environ.get("PREFILL_MAX_RETRIES", "4"))


# ----------------------------------------------------------------------
# Scenario I: overlapping date range detection
# When two files for the same Job Number cover overlapping periods, use
# only the one with the broader range to avoid double-counting hours.
# ----------------------------------------------------------------------
def _parse_date_range(text: str):
    """Extract (start, end) as comparable YYYYMMDD strings from document text.
    Looks for patterns like 'From 01/01/25 To 10/31/25' or 'From: 01/01/2025'.
    Returns (None, None) if no date range is found.
    """
    m = re.search(
        r"From[\s:]+(\d{1,2}/\d{1,2}/\d{2,4})\s+To[\s:]+(\d{1,2}/\d{1,2}/\d{2,4})",
        text, re.I,
    )
    if not m:
        return None, None

    def _norm(d: str) -> str:
        parts = d.split("/")
        mm, dd = parts[0].zfill(2), parts[1].zfill(2)
        yy = parts[2]
        yyyy = ("20" + yy) if len(yy) == 2 else yy
        return f"{yyyy}{mm}{dd}"

    return _norm(m.group(1)), _norm(m.group(2))


def _deduplicate_overlapping_ranges(extracted: List[tuple], lg=None) -> List[tuple]:
    """Scenario I: for each Job Number, keep only the file(s) covering the
    broadest date range. Files with non-overlapping ranges are all kept.
    Files that are a strict subset of another file's range are dropped.

    extracted: [(name, text, meta), ...]
    Returns filtered list with a warning list.
    """
    from difflib import SequenceMatcher

    # Extract job number + date range per file
    file_info: List[dict] = []
    for name, text, meta in extracted:
        job_m = re.search(r"Job[\s:]+(\d+)\b", text[:2000], re.I)
        job_num = job_m.group(1).strip() if job_m else None
        start, end = _parse_date_range(text[:2000])
        file_info.append({"name": name, "job": job_num, "start": start, "end": end})

    skipped: set[str] = set()
    overlap_warnings: List[str] = []

    # Group by job number and check for subset ranges
    job_files: dict[str, List[int]] = {}
    for i, fi in enumerate(file_info):
        if fi["job"] and fi["start"] and fi["end"]:
            job_files.setdefault(fi["job"], []).append(i)

    for job_num, indices in job_files.items():
        if len(indices) < 2:
            continue
        for i in indices:
            for j in indices:
                if i == j or file_info[i]["name"] in skipped:
                    continue
                # Is file i a strict subset of file j's date range?
                if (file_info[j]["start"] <= file_info[i]["start"] and
                        file_info[j]["end"] >= file_info[i]["end"] and
                        (file_info[j]["start"] != file_info[i]["start"] or
                         file_info[j]["end"] != file_info[i]["end"])):
                    skipped.add(file_info[i]["name"])
                    overlap_warnings.append(
                        f"Scenario I — Overlapping date ranges for Job {job_num}: "
                        f"'{file_info[i]['name']}' "
                        f"({file_info[i]['start'][:4]}/{file_info[i]['start'][4:6]}"
                        f"–{file_info[i]['end'][:4]}/{file_info[i]['end'][4:6]}) "
                        f"is a subset of '{file_info[j]['name']}' "
                        f"({file_info[j]['start'][:4]}/{file_info[j]['start'][4:6]}"
                        f"–{file_info[j]['end'][:4]}/{file_info[j]['end'][4:6]}). "
                        f"Skipping to avoid double-counting hours.")

    if skipped and lg:
        for w in overlap_warnings:
            lg.log(f"  ⚠ {w}")

    result = [(n, t, m) for n, t, m in extracted if n not in skipped]
    return result, overlap_warnings


# ----------------------------------------------------------------------
# WF guardrails: wrong-file detection (Part 12 / Scenarios WF-1 to WF-4)
# ----------------------------------------------------------------------

# Keywords that strongly suggest a document contains R&D project data.
_RD_SIGNALS_Q2 = [
    r"\bhours?\b", r"\bman.?hours?\b", r"\bpayroll\b", r"\blabor\b",
    r"\bemployee\b", r"\bphase\b", r"\bjob\b", r"\bproject\b",
    r"\btechnical\b", r"\buncertainty\b", r"\bexperimentation\b",
    r"\br&d\b", r"\bresearch\b", r"\bdevelopment\b", r"\bcost\b",
    r"\bqualified\b", r"\btax credit\b", r"\birc.*41\b",
]

# Keywords that strongly suggest a generic/non-project document.
_GENERIC_SIGNALS = [
    r"\bhandbook\b", r"\bonboarding\b", r"\btravel policy\b",
    r"\bcode of conduct\b", r"\bbenefits\b", r"\bvacation\b",
    r"\bbalance sheet\b", r"\bincome statement\b", r"\bp&l\b",
    r"\bprofit.{0,5}loss\b", r"\bannual report\b",
]


def _wrong_file_test(name: str, text: str) -> tuple[bool, str]:
    """Run the 3-question wrong-file test from Part 12 / WF guardrails.

    Q1: Does this file contain a named project, job, product, or initiative?
    Q2: Does this file contain hours, costs, employees, or technical R&D content?
    Q3: Is the content specific to an identifiable entity (not generic policy)?

    Returns (is_valid_rd_file, reason_if_invalid).
    """
    sample = text[:3000].lower()

    # Fast path: file is nearly empty (scanned / corrupt / unreadable)
    if len(text.strip()) < 50:
        return False, (
            f"File '{name}' contains almost no text. "
            "It may be a scanned image, password-protected, or corrupt. "
            "Please upload a text-selectable version.")

    # Q3 fast-fail: obvious generic/policy document
    for pat in _GENERIC_SIGNALS:
        if re.search(pat, sample, re.I):
            return False, (
                f"File '{name}' appears to be a general company document "
                f"(HR policy, financial statement, or similar), not an R&D project file. "
                "Please upload project cost reports, payroll records, or technical documents.")

    # Q1: does it reference a named project or job?
    q1 = bool(
        re.search(r"job\s*[:#]?\s*\d+|project\s*[:#]|contract\s*[:#]|"
                  r"project name|business component|r&d.{0,10}study", sample, re.I)
    )

    # Q2: does it contain R&D-relevant data signals?
    q2 = sum(1 for pat in _RD_SIGNALS_Q2 if re.search(pat, sample, re.I)) >= 3

    if not q1 and not q2:
        return False, (
            f"File '{name}' does not appear to contain R&D project data. "
            "No project identifiers, hours, employees, or technical R&D content "
            "were found. Please upload project cost reports, payroll records, "
            "proposals, or R&D study documents.")

    return True, ""


# ----------------------------------------------------------------------
# Stage 1: extraction (runs inside a separate process; must be top-level)
# ----------------------------------------------------------------------
def _extract_worker(path: str) -> tuple:
    r = extract(path)
    name = os.path.basename(path)
    return (name, r.text, {"pages": r.page_count, "chars": r.char_count,
                           "needs_ocr": r.needs_ocr, "method": r.method})


# ----------------------------------------------------------------------
# Stage 3: one LLM call (async), cached, with retry/backoff
# ----------------------------------------------------------------------
@dataclass
class Telemetry:
    llm_calls: int = 0
    cache_hits: int = 0
    retries: int = 0
    sections: int = 0
    chunks: int = 0


async def _llm_call(text: str, tel: Telemetry, lg=None, tag: str = "") -> dict:
    """Cache-checked, retrying LLM call returning {'projects': [...]}.
    Uses AsyncOpenAI when OPENAI_API_KEY is set, else the offline mock."""
    key = "llm_" + _sha256_text(text, MODEL, PROMPT_VERSION, SCHEMA_VERSION,
                                json.dumps(PROJECT_JSON_SCHEMA, sort_keys=True))
    if lg:
        lg.save_text(f"03_llm/{tag}_input.txt", text)
    cached = _cache_get(key)
    if cached is not None:
        tel.cache_hits += 1
        if lg:
            lg.log(f"    LLM {tag}: CACHE HIT ({len(text)} chars) -> "
                   f"{len(cached.get('projects', []))} project(s)")
            lg.save_json(f"03_llm/{tag}_output.json", {**cached, "_cache_hit": True})
        return cached

    mode = "mock" if not os.environ.get("OPENAI_API_KEY") else MODEL
    if lg:
        lg.log(f"    LLM {tag}: calling [{mode}] with {len(text)} chars (~{len(text)//4} tokens)…")
    if not os.environ.get("OPENAI_API_KEY"):
        result = _mock_llm(text)                    # offline path
        await asyncio.sleep(float(os.environ.get("PREFILL_MOCK_LATENCY", "0.05")))
    else:
        result = await _openai_with_retry(text, tel)

    _cache_put(key, result)
    tel.llm_calls += 1
    if lg:
        lg.log(f"    LLM {tag}: done -> {len(result.get('projects', []))} project(s)")
        lg.save_json(f"03_llm/{tag}_output.json", result)
    return result


async def _openai_with_retry(text: str, tel: Telemetry) -> dict:
    from openai import AsyncOpenAI
    client = AsyncOpenAI()
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.responses.create(
                model=MODEL,
                input=[{"role": "system", "content": SYSTEM_PROMPT},
                       {"role": "user", "content": text}],
                text={"format": {"type": "json_schema", "name": "stage9_projects",
                                 "schema": PROJECT_JSON_SCHEMA, "strict": True}},
            )
            return json.loads(resp.output_text)
        except Exception as e:                       # rate limit / transient
            if attempt == MAX_RETRIES - 1:
                raise
            tel.retries += 1
            await asyncio.sleep(delay)
            delay *= 2                                # exponential backoff
    return {"projects": []}


# ----------------------------------------------------------------------
# Map-reduce for an oversized single-project section
# ----------------------------------------------------------------------
def _reduce_partials(partials: List[dict]) -> dict:
    """Merge per-chunk extractions of the SAME project into one record:
    longest non-empty string wins; max for numeric/confidence fields."""
    projects = [p for part in partials for p in part.get("projects", [])]
    if not projects:
        return {}
    merged = dict(projects[0])
    for p in projects[1:]:
        for k, v in p.items():
            cur = merged.get(k)
            if isinstance(v, str):
                if len(v.strip()) > len(str(cur or "").strip()):
                    merged[k] = v
            elif isinstance(v, (int, float)) and v is not None:
                merged[k] = max(cur or 0, v)
            elif cur in (None, ""):
                merged[k] = v
    return merged


# Fields whose absence makes a project worth a human review flag.
KEY_FIELDS = ["description", "technical_challenges_uncertainties",
              "solutions_alternatives_considered"]
REVIEW_CONF = float(os.environ.get("PREFILL_REVIEW_CONF", "0.7"))


def _title_key(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def merge_projects_by_title(projects: List[dict], threshold: float = 0.82) -> List[dict]:
    """Merge projects that are the SAME across chunk boundaries, matched by fuzzy
    title similarity. Used by the llm_detect path where one project can appear in
    multiple overlapping chunks.

    Job Number is the authoritative dedup key: if two records both have explicit
    Job Numbers and those numbers differ, they are always DIFFERENT projects —
    fuzzy similarity is skipped to prevent geographically similar names
    (e.g. 'West South Bay' vs 'East South Bay') from being incorrectly merged.
    """
    from difflib import SequenceMatcher
    merged: List[dict] = []
    for p in projects:
        tk = _title_key(p.get("project_title", ""))
        pjn = re.search(r"\bjob\s*(\d+)\b", p.get("project_title", ""), re.I)
        hit = None
        for m in merged:
            mk = _title_key(m.get("project_title", ""))
            mjn = re.search(r"\bjob\s*(\d+)\b", m.get("project_title", ""), re.I)
            # If both have explicit Job Numbers, use number as the sole dedup key
            if pjn and mjn:
                if pjn.group(1) == mjn.group(1):
                    hit = m; break    # same Job Number → merge
                else:
                    continue          # different Job Numbers → never merge
            # Fallback: fuzzy check when one/both titles have no Job Number
            if tk and mk and (tk == mk or SequenceMatcher(None, tk, mk).ratio() >= threshold):
                hit = m; break
        if hit:
            _merge_into(hit, p)
        else:
            merged.append(dict(p))
    return merged


def _merge_into(base: dict, other: dict):
    """Field-level merge: longest non-empty string, max number, max confidence."""
    for k, v in other.items():
        cur = base.get(k)
        if isinstance(v, str):
            if len(v.strip()) > len(str(cur or "").strip()):
                base[k] = v
        elif isinstance(v, (int, float)) and v is not None:
            base[k] = max(cur or 0, v)
        elif cur in (None, ""):
            base[k] = v


_SUBSTANCE = ["description", "technical_challenges_uncertainties",
              "solutions_alternatives_considered", "supplies_used"]


# ---------------------------------------------------------------------------
# Canonical project list extraction + post-filter
# ---------------------------------------------------------------------------

def _extract_canonical_projects(texts: List[str]) -> List[str]:
    """Regex scan across all extracted document texts to build a whitelist of
    canonical top-level project titles.

    Detects four document formats:
      A - Narrative study (dash):       'Job 314 - Coyote Flood Management'
                                         or 'Project Name: Job 314 - ...'
      B - Cost-report header:            'Job: 314   COYOTE FLOOD MANAGEMENT'
      C - Parenthetical (Occams multi):  'the Foo Bar project (Job 356)'
                                         or 'Project N: Foo Bar (Job 356)'
      D - Comma-list executive summary:  'Jobs 314, 317, 338, 350, 351, and 356'
          (for D we only get numbers; names are filled in by any of A/B/C that
           match the same number, or left as 'Job NNN' if no name found)

    Returns a deduplicated list of 'Job NNN - Title Case Name' strings.
    If no job-numbered projects are detected the list is empty and the caller
    skips filtering (so the pipeline still works on non-Job-Number documents).
    """
    from difflib import SequenceMatcher
    combined = "\n".join(texts)
    found: List[str] = []

    # Pattern A — "Job NNN - Name" on its own or inside "Project Name: Job NNN - Name"
    for m in re.finditer(
        r"Job[\s:]+(\d+)\s*[-–]\s*([A-Za-z][^\n\|\r]{3,70}?)(?=\s{2,}|\s*\||\s*Qualified|\s*Not Qualified|\n|$)",
        combined, re.I,
    ):
        num = m.group(1).strip()
        name = m.group(2).strip().rstrip(".,;:")
        if len(re.sub(r"[^A-Za-z]", "", name)) >= 4:
            found.append(f"Job {num} - {name.title()}")

    # Pattern B — cost-report header "Job: 314   COYOTE FLOOD MANAGEMENT"
    for m in re.finditer(
        r"Job:\s*(\d+)\s{2,}([A-Z][A-Z0-9 &,'-]{3,60}?)(?=\s*$|\s{2,}|\n)",
        combined, re.M,
    ):
        num = m.group(1).strip()
        name = m.group(2).strip().rstrip(".,;:")
        if len(re.sub(r"[^A-Za-z]", "", name)) >= 4:
            found.append(f"Job {num} - {name.title()}")

    # Pattern E — structured project section headers (highest quality, no prose context)
    # Matches two header formats used in Occams multi-year studies:
    #   "Project N: Coyote Flood Management — Sheet Pile Flood Control Wall (Job 314)"
    #   "Project: Coyote Flood Management — Sheet Pile Flood Control Wall (Job 314)"
    #   "Project: Tunitas Creek Beach Improvements — ADA Pathway and Handrail\nSystem (Job 317)"
    # Takes ONLY the primary name before the em-dash.
    # Pre-processing: join lines where "(Job NNN)" falls on the line immediately
    # after the project name because of PDF column-wrapping.
    joined = re.sub(
        r"([^\n]{10,})\n([^\n]{1,40})\s*(\(Job\s+\d+\))",
        lambda m: m.group(1) + " " + m.group(2) + " " + m.group(3),
        combined,
    )

    _e_hits: set[str] = set()   # job numbers already captured by Pattern E
    for m in re.finditer(
        r"Project\s*(?:\d+\s*)?[:\-]\s*"     # "Project N:" or "Project:"
        r"([A-Za-z][^\n—–(]{4,80}?)"         # primary project name (stops at em-dash)
        r"\s*(?:[—–][^\n(]*)?"               # optional " — sub-description"
        r"\s*\(Job\s+(\d+)\)",               # (Job NNN)
        joined, re.I,
    ):
        name_raw = m.group(1).strip().rstrip("—–-:, ")
        num = m.group(2).strip()
        if len(re.sub(r"[^A-Za-z]", "", name_raw)) >= 4:
            found.append(f"Job {num} - {name_raw.title()}")
            _e_hits.add(num)

    # Pattern C — generic parenthetical format "Some Name (Job 356)"
    # Used as FALLBACK when Pattern E did not find a name for this job number.
    # Non-greedy match keeps it short; leading articles stripped.
    # Uses `joined` (line-continuation-fixed) text for the same multi-line benefit.
    for m in re.finditer(
        r"([A-Za-z][^\n\(\)]{4,80}?)\s*\(Job\s+(\d+)\)",
        joined, re.I,
    ):
        name_raw = m.group(1).strip().rstrip("—–-:, ")
        num = m.group(2).strip()
        if num in _e_hits:
            continue           # Pattern E already has a better name for this job
        # Strip leading articles only (no hardcoded company/verb lists)
        name_raw = re.sub(r"^(?:the\s+|a\s+|an\s+)", "", name_raw, flags=re.I).strip()
        if len(re.sub(r"[^A-Za-z]", "", name_raw)) >= 4:
            found.append(f"Job {num} - {name_raw.title()}")

    # Pattern D — executive summary comma list "Jobs 314, 317, 338, 350, 351, and 356"
    # Extracts job numbers only; names come from A/B/C matches or stay as "Job NNN"
    for m in re.finditer(
        r"Jobs?\s+((?:\d{3,4}(?:\s*,\s*|\s+and\s+))+\d{3,4})",
        combined, re.I,
    ):
        nums = re.findall(r"\d{3,4}", m.group(1))
        for num in nums:
            found.append(f"__num_only_{num}__")

    # Resolve Pattern D placeholders: if a named entry already exists for that
    # job number, the placeholder is redundant and gets dropped. If no named
    # entry exists, convert the placeholder to a bare "Job NNN" entry so the
    # canonical filter at least knows that project exists in the document.
    named = [f for f in found if not f.startswith("__num_only_")]
    placeholders = [f for f in found if f.startswith("__num_only_")]
    for ph in placeholders:
        num = ph.replace("__num_only_", "").replace("__", "")
        already_named = any(
            bool(re.search(r"\bjob\s*" + re.escape(num) + r"\b", n, re.I))
            for n in named
        )
        if not already_named:
            named.append(f"Job {num}")

    # Deduplicate — keep first occurrence of each unique project.
    # Job Number is the authoritative dedup key: if BOTH entries have an
    # explicit Job Number and those numbers are different, they are ALWAYS
    # different projects — skip fuzzy similarity entirely to prevent
    # geographically similar names (e.g. "West South Bay" vs "East South Bay")
    # from being incorrectly merged.
    result: List[str] = []
    for title in named:
        tk = _title_key(title)
        jn = re.search(r"\bjob\s*(\d+)\b", title, re.I)
        already = False
        for r in result:
            rn = re.search(r"\bjob\s*(\d+)\b", r, re.I)
            if jn and rn:
                if jn.group(1) == rn.group(1):
                    already = True; break    # same Job Number → duplicate
                else:
                    continue                  # different Job Numbers → different project, skip fuzzy
            # Fallback: fuzzy check only when one/both entries have no Job Number
            if SequenceMatcher(None, tk, _title_key(r)).ratio() >= 0.85:
                already = True; break
        if not already:
            result.append(title)
    return result


def _best_title(llm_title: str, canonical: str) -> str:
    """Return the best project title.

    The canonical is the authoritative title produced by the regex extractor
    from structured parts of the document. It always wins UNLESS it is a bare
    'Job NNN' placeholder (no name found at all), in which case the LLM title
    is used if it carries the same Job Number.
    """
    lt = (llm_title or "").strip()
    cn = (canonical or "").strip()

    if not lt:
        return cn

    # Check if canonical is a bare "Job NNN" with no real project name
    cn_is_bare = bool(re.fullmatch(r"Job\s+\d+", cn.strip(), re.I))

    if cn_is_bare:
        # No regex-extracted name exists — use LLM title if same Job Number
        lt_jn = re.search(r"\bjob\s*(\d+)\b", lt, re.I)
        cn_jn = re.search(r"\bjob\s*(\d+)\b", cn, re.I)
        if lt_jn and cn_jn and lt_jn.group(1) == cn_jn.group(1):
            return lt

    # Canonical has a real name — it wins.
    return cn


def _filter_to_canonical(
    projects: List[dict],
    doc_canonicals: dict,    # {doc_name: [canonical title, ...]}
    all_canonical: List[str],  # deduplicated global canonical list
) -> List[dict]:
    """Per-document canonical filtering + cross-document deduplication.

    Phase 1 — per-doc filtering:
      • Projects from a doc WITH a canonical list (Job-Number documents) →
        keep only those whose title matches the doc's canonical list.
        Unmatched titles are false positives (sub-section headings, phase names)
        and are dropped.
      • Projects from a doc WITHOUT a canonical list (non-Job-Number narrative
        PDFs, memos, etc.) → pass ALL through unchanged.

    Phase 2 — cross-doc deduplication:
      • Same project detected in multiple files → merge into one record using
        fuzzy title matching (threshold 0.72 or exact Job Number match).
      • Canonical title is always restored after merging so LLM-invented longer
        strings can never overwrite the authoritative name.

    Returns projects sorted: canonical projects first (in detected order),
    followed by any non-canonical projects.
    """
    from difflib import SequenceMatcher

    THRESHOLD = 0.72

    def _best_match(title: str, canon_list: List[str]):
        """Return (canonical_title, score) against a given canon list."""
        tk = _title_key(title)
        jn = re.search(r"\bjob\s*(\d+)\b", title, re.I)
        best_c, best_s = None, 0.0
        for c in canon_list:
            cn = re.search(r"\bjob\s*(\d+)\b", c, re.I)
            if jn and cn and jn.group(1) == cn.group(1):
                return c, 1.0          # definitive job-number match
            s = SequenceMatcher(None, tk, _title_key(c)).ratio()
            if s > best_s:
                best_s = s; best_c = c
        return (best_c, best_s) if best_s >= THRESHOLD else (None, 0.0)

    # ------------------------------------------------------------------ #
    # Phase 1: per-document filter                                         #
    # ------------------------------------------------------------------ #
    kept: List[dict] = []
    for p in projects:
        source = p.get("_source_file", "")
        doc_canon = doc_canonicals.get(source, [])
        if doc_canon:
            # This document has Job-Number projects — filter false positives.
            c, _ = _best_match(p.get("project_title", ""), doc_canon)
            if c:
                p_copy = dict(p)
                # Use the RICHER of the canonical name and the LLM's title.
                # Canonical guarantees the correct Job Number; LLM often has
                # the fuller human-readable name. Pick whichever has more
                # alphabetic content, unless the LLM title is a placeholder.
                p_copy["project_title"] = _best_title(
                    p_copy.get("project_title", ""), c)
                kept.append(p_copy)
            # else: silent drop — false positive (phase name, sub-heading, etc.)
        else:
            # No canonical list for this doc — pass through as-is.
            kept.append(p)

    # ------------------------------------------------------------------ #
    # Phase 2: cross-document deduplication                               #
    # ------------------------------------------------------------------ #
    # Normalise all titles to the best available canonical+LLM title
    # BEFORE merging so that variations collapse into one record.
    for p in kept:
        c, _ = _best_match(p.get("project_title", ""), all_canonical)
        if c:
            p["project_title"] = _best_title(p.get("project_title", ""), c)

    # Now merge — projects with identical (or near-identical) titles collapse.
    merged = merge_projects_by_title(kept, threshold=THRESHOLD)

    # Re-apply best-title selection after merge in case _merge_into overwrote.
    for p in merged:
        c, _ = _best_match(p.get("project_title", ""), all_canonical)
        if c:
            p["project_title"] = _best_title(p.get("project_title", ""), c)

    # Sort: canonical projects first (in canonical order), then others.
    def _sort_key(p):
        title = p.get("project_title", "")
        c, s = _best_match(title, all_canonical)
        return (all_canonical.index(c) if c and c in all_canonical
                else len(all_canonical))

    merged.sort(key=_sort_key)
    return merged


# Internal placeholder values that should never reach the UI as a project title.
_TITLE_PLACEHOLDERS = {"(auto-detect projects)", "auto-detect projects", "untitled project", ""}


def _is_real_project(p: dict) -> bool:
    """Guard against empty/junk records (stray LLM objects, table fragments,
    internal pipeline placeholders).

    A record is kept only when ALL of the following hold:
      1. project_title is a real name — not empty and not an internal placeholder.
      2. The title has at least 3 alphabetic characters.
      3. At least one substantive content field is non-empty OR numeric fields
         are present — a title alone with no supporting content is dropped.
    """
    raw_title = (p.get("project_title") or "").strip()
    # Reject internal placeholder titles that the pipeline injects as fallbacks.
    if raw_title.lower() in _TITLE_PLACEHOLDERS:
        return False
    alpha_title = re.sub(r"[^A-Za-z]", "", raw_title)
    if len(alpha_title) < 3:
        return False
    has_text = any(
        str(p.get(f) or "").strip() and p.get(f) != _NOT_FOUND_MARKER
        for f in _SUBSTANCE
    )
    has_num = p.get("total_man_hours") or p.get("employees_completing_research")
    return bool(has_text or has_num)


# String fields that should show "[Not found in uploaded documents]" rather
# than empty string when the LLM couldn't find them (Scenario F).
_NOT_FOUND_MARKER = "[Not found in uploaded documents]"
_TEXT_FIELDS = [
    "description", "technical_challenges_uncertainties",
    "solutions_alternatives_considered", "supplies_used",
    "contract_type",
]


def _apply_not_found_markers(p: dict) -> dict:
    """Scenario F: Replace empty string fields with the not-found marker so
    the UI can clearly distinguish 'AI left this blank intentionally' from
    'this field was not populated' — preventing silent blank submissions."""
    for field in _TEXT_FIELDS:
        val = p.get(field)
        if isinstance(val, str) and not val.strip():
            p[field] = _NOT_FOUND_MARKER
    return p


def _flag_scenario_j(p: dict):
    """Scenario J: If the project title has no Job Number / Project Number,
    flag it and cap confidence at 0.7 (moderate tier) since the identifier
    is ambiguous — we are using the project name itself as the dedup key."""
    title = p.get("project_title") or ""
    has_job_number = bool(re.search(r"\bjob\s*\d+\b|\bproject\s*#?\s*\w+\b|\bcontract\s+\w+", title, re.I))
    if not has_job_number:
        p["_no_job_number"] = True
        p["_identifier_note"] = (
            "[No Job Number found — using project name as identifier. "
            "Confidence capped at 70%. Please verify project identity.]"
        )
        # Cap confidence at 0.7 as per Scenario J spec (60-70% range)
        if (p.get("confidence") or 0) > 0.70:
            p["confidence"] = 0.70
    else:
        p["_no_job_number"] = False
        p["_identifier_note"] = ""


def _flag_review(p: dict):
    reasons = []
    if (p.get("confidence") or 0) < REVIEW_CONF:
        reasons.append("low confidence")
    # Check against the marker as well as empty string
    missing = [f for f in KEY_FIELDS
               if not str(p.get(f) or "").strip()
               or p.get(f) == _NOT_FOUND_MARKER]
    if missing:
        reasons.append("missing: " + ", ".join(missing))
    if p.get("_no_job_number"):
        reasons.append("no job number — identifier is project name only")
    p["_needs_review"] = bool(reasons)
    p["_review_reasons"] = reasons


async def _process_section(sec: Section, sem: asyncio.Semaphore,
                           tel: Telemetry, progress: Optional[Callable], lg=None) -> List[dict]:
    async def guarded(t, tag):
        async with sem:                              # cap concurrent LLM calls
            return await _llm_call(t, tel, lg, tag)

    if lg:
        lg.log(f"  Section {sec.index} [{sec.strategy}] {sec.doc} {sec.pages} "
               f"~{sec.token_est} tok"
               + (f", {len(sec.chunks)} chunks" if sec.needs_chunking else ""))

    if sec.strategy == "llm_detect":
        # format-agnostic fallback: detect projects in EACH chunk, merge by title
        tel.chunks += len(sec.chunks)
        partials = await asyncio.gather(
            *[guarded(c, f"s{sec.index}_chunk{i}") for i, c in enumerate(sec.chunks)])
        found = [p for part in partials for p in part.get("projects", [])]
        projects = merge_projects_by_title(found)
        if lg:
            lg.log(f"    merged {len(found)} chunk-projects -> {len(projects)} unique")
    elif sec.needs_chunking:
        # structural section too big for one call = ONE project across chunks
        tel.chunks += len(sec.chunks)
        partials = await asyncio.gather(
            *[guarded(c, f"s{sec.index}_chunk{i}") for i, c in enumerate(sec.chunks)])
        project = _reduce_partials(partials)
        projects = [project] if project else []
    else:
        out = await guarded(sec.text, f"s{sec.index}")
        projects = out.get("projects", [])

    for p in projects:                               # provenance + review flags
        p["_source_file"] = sec.doc
        p["_source_pages"] = sec.pages
        p["_detection"] = sec.strategy
        # Do NOT fall back to the internal title_hint placeholder — an empty
        # project_title means the LLM couldn't identify a project name, and
        # _is_real_project() will correctly drop this record downstream.
        _apply_not_found_markers(p)                  # Scenario F
        _flag_scenario_j(p)                          # Scenario J
        _flag_review(p)
    if progress:
        progress(sec)
    return projects


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------
async def run_batch(paths: List[str], concurrency: int = MAX_CONCURRENCY,
                    progress: Optional[Callable] = None, log: bool = True) -> dict:
    t0 = time.time()
    tel = Telemetry()
    loop = asyncio.get_event_loop()

    lg = None
    if log:
        from logger import RunLogger
        lg = RunLogger(label=f"{len(paths)} file(s)")
        mode = MODEL if os.environ.get("OPENAI_API_KEY") else "OFFLINE MOCK (no API key)"
        lg.step("1/4", f"Received {len(paths)} file(s). LLM backend: {mode}")
        for p in paths:
            lg.log(f"      - {os.path.basename(p)}")

    # Stage 1: parallel extraction across files (processes)
    with ProcessPoolExecutor() as pool:
        extracted = await asyncio.gather(
            *[loop.run_in_executor(pool, _extract_worker, p) for p in paths])
    if lg:
        lg.step("2/4", "Extraction complete (PyMuPDF/pdfplumber/docx, in parallel):")
        for name, text, meta in extracted:
            lg.log(f"      {name}: {meta['pages']}p, {meta['chars']} chars, "
                   f"needs_ocr={meta['needs_ocr']} [{meta['method']}]")
            lg.save_text(f"01_extracted/{name}.txt", text)

    # WF guardrails: 3-question wrong-file detection. Filter out files that
    # clearly contain no R&D project data (policy docs, P&L statements, etc.)
    wf_warnings: List[str] = []
    valid_extracted = []
    for item in extracted:
        name, text, meta = item
        ok, reason = _wrong_file_test(name, text)
        if ok:
            valid_extracted.append(item)
        else:
            wf_warnings.append(reason)
            if lg:
                lg.log(f"  ⚠ WF: {reason}")
    extracted = valid_extracted
    if not extracted:
        return {
            "projects": [],
            "warnings": wf_warnings,
            "message": (
                "No R&D project data was found in any of the uploaded files. "
                + " ".join(wf_warnings)),
            "telemetry": {"files": len(paths), "projects": 0,
                          "elapsed_ms": int((time.time() - t0) * 1000)},
            "extraction": {},
        }

    # Scenario I: drop files whose date range is a strict subset of another
    # file for the same Job Number — prevents double-counting labor hours.
    extracted, overlap_warnings = _deduplicate_overlapping_ranges(list(extracted), lg)
    if overlap_warnings and lg:
        lg.log(f"  Overlap dedup: {len(overlap_warnings)} file(s) skipped.")

    # Stage 2: segment into sections
    sections = segment_batch([(name, text) for name, text, _ in extracted])
    tel.sections = len(sections)
    if lg:
        lg.step("3/4", f"Segmented into {len(sections)} section(s):")
        lg.save_json("02_segmentation.json", [
            {"doc": s.doc, "index": s.index, "strategy": s.strategy,
             "token_est": s.token_est, "pages": s.pages,
             "chunks": len(s.chunks), "title_hint": s.title_hint} for s in sections])

    # Stage 3: bounded-concurrency prefill across all sections
    sem = asyncio.Semaphore(concurrency)
    if lg:
        lg.log(f"  Running LLM prefill, max {concurrency} concurrent calls…")
    per_section = await asyncio.gather(
        *[_process_section(s, sem, tel, progress, lg) for s in sections])

    # Stage 4: aggregate + drop empty/junk records
    projects = [p for group in per_section for p in group if _is_real_project(p)]

    # Stage 4b: per-document canonical filter + cross-document deduplication.
    # For each file, detect its canonical Job-Number project list. Documents
    # that have a canonical list get false-positive filtering; documents without
    # one (narrative PDFs without Job Numbers) pass all their projects through.
    doc_canonicals = {name: _extract_canonical_projects([text])
                      for name, text, _ in extracted}

    # Build deduplicated global canonical list (across all docs).
    all_canonical: List[str] = []
    _seen_job_nums: set = set()
    for cs in doc_canonicals.values():
        for c in cs:
            jn = re.search(r"\bjob\s*(\d+)\b", c, re.I)
            key = jn.group(1) if jn else _title_key(c)
            if key not in _seen_job_nums:
                _seen_job_nums.add(key)
                all_canonical.append(c)

    if any(doc_canonicals.values()):
        if lg:
            lg.log(f"  Canonical project list ({len(all_canonical)}): "
                   + ", ".join(f'"{c}"' for c in all_canonical))
            docs_without = [n for n, cs in doc_canonicals.items() if not cs]
            if docs_without:
                lg.log(f"  Pass-through docs (no Job-Number format): "
                       + ", ".join(docs_without))
        projects = _filter_to_canonical(projects, doc_canonicals, all_canonical)

    needs_review = sum(1 for p in projects if p.get("_needs_review"))
    strategies = {}
    for s in sections:
        strategies[s.strategy] = strategies.get(s.strategy, 0) + 1
    result = {
        "projects": projects,
        "telemetry": {**asdict(tel), "files": len(paths),
                      "projects": len(projects), "needs_review": needs_review,
                      "strategies": strategies,
                      "elapsed_ms": int((time.time() - t0) * 1000)},
        "extraction": {name: meta for name, _, meta in extracted},
        "warnings": overlap_warnings + wf_warnings,
    }

    if lg:
        lg.step("4/4", f"Done: {len(projects)} project(s), {needs_review} flagged for "
                f"review, {tel.llm_calls} LLM call(s), {tel.cache_hits} cache hit(s), "
                f"{result['telemetry']['elapsed_ms']}ms")
        for p in projects:
            flag = " ⚠REVIEW" if p.get("_needs_review") else ""
            lg.log(f"      • [{p.get('tax_year')}] {p.get('project_title','?')[:50]} "
                   f"(mh={p.get('total_man_hours')}, conf={p.get('confidence')}){flag}")
        lg.save_json("04_result.json", result)
        lg.log(f"Full logs saved to: {lg.dir}")
        result["telemetry"]["run_id"] = lg.run_id
        result["telemetry"]["log_dir"] = lg.dir

    return result


# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    paths = sys.argv[1:]
    if not paths:
        print("usage: python3 pipeline.py file1.pdf [file2.pdf ...]"); raise SystemExit

    def show(sec):
        print(f"  ✓ {sec.doc} {sec.pages} ~{sec.token_est} tok | {sec.title_hint[:55]!r}")

    out = asyncio.run(run_batch(paths, progress=show))
    print("\n--- projects ---")
    for i, p in enumerate(out["projects"], 1):
        print(f"[{i}] {p.get('project_title','?')[:60]!r}  "
              f"({p.get('_source_file')} {p.get('_source_pages')})  "
              f"man_hrs={p.get('total_man_hours')} conf={p.get('confidence')}")
    print("\n--- telemetry ---")
    print(json.dumps(out["telemetry"], indent=2))
