"""
Industry and science-discipline detection for the Stage-9 prefill pipeline.

How it works
------------
1. detect_industry(text)
   Scans the project's extracted document text for the industry whose
   stage1_values keywords appear most frequently. Returns the industry key
   (e.g. "architecture_construction") or "fallback_general" if no match.

2. detect_disciplines(industry_key, text)
   For every discipline under the detected industry, counts how many unique
   keyword_cluster entries appear in the text. If the count meets the
   discipline's match_threshold the discipline is auto_checked = True.

3. enrich_project(project, full_doc_text)
   Convenience wrapper: takes a project dict and the raw extracted text,
   adds "_industry", "_industry_label", and "_disciplines" fields in-place,
   then returns the dict.

The UI uses _disciplines to render the multi-select pill list, with
auto_checked items pre-selected and the rest available to add.
"""
from __future__ import annotations
import json
import os
import re
from typing import List, Dict, Optional

# ── Load map once at import time ──────────────────────────────────────────────
_MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "industry_discipline_map.json")
with open(_MAP_PATH, encoding="utf-8") as _f:
    _MAP: dict = json.load(_f)

_INDUSTRIES: dict = _MAP["industries"]
_FALLBACK: dict = _MAP["fallback_general"]
_DEFAULT_THRESHOLD: int = _MAP["_meta"]["match_threshold_default"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """Lowercase, collapse whitespace for consistent matching."""
    return re.sub(r"\s+", " ", text.lower().strip())


def _count_keyword_hits(keywords: List[str], text_norm: str,
                        threshold: int) -> int:
    """Count unique keyword hits in normalised text.
    Each keyword counts once regardless of how many times it appears.
    Returns the count of matched unique keywords."""
    hits = 0
    for kw in keywords:
        kw_norm = _norm(kw)
        # whole-word / whole-phrase match
        pattern = r"(?<![a-z0-9])" + re.escape(kw_norm) + r"(?![a-z0-9])"
        if re.search(pattern, text_norm):
            hits += 1
    return hits


# ── Public API ────────────────────────────────────────────────────────────────

def detect_industry(text: str) -> str:
    """Return the best-matching industry key for the given document text.

    Scans stage1_values for each industry. The industry whose keywords appear
    most often wins. Returns "fallback_general" when no industry reaches 1 hit.
    """
    text_norm = _norm(text)
    best_key   = "fallback_general"
    best_score = 0

    for industry_key, industry_data in _INDUSTRIES.items():
        score = 0
        for kw in industry_data.get("stage1_values", []):
            kw_norm = _norm(kw)
            pattern = r"(?<![a-z0-9])" + re.escape(kw_norm) + r"(?![a-z0-9])"
            if re.search(pattern, text_norm):
                score += 1
        if score > best_score:
            best_score = score
            best_key   = industry_key

    return best_key if best_score > 0 else "fallback_general"


def detect_disciplines(industry_key: str, text: str) -> List[dict]:
    """Return a list of discipline dicts with auto_checked populated.

    Each dict has:
        id            – discipline identifier
        label         – human-readable label
        auto_checked  – True if keyword hits >= match_threshold
        matched_count – number of unique keywords found (for debugging)
    """
    text_norm = _norm(text)

    if industry_key == "fallback_general" or industry_key not in _INDUSTRIES:
        discipline_defs = _FALLBACK.get("disciplines", [])
    else:
        discipline_defs = _INDUSTRIES[industry_key].get("disciplines", [])

    results = []
    for disc in discipline_defs:
        threshold = disc.get("match_threshold", _DEFAULT_THRESHOLD)
        hits = _count_keyword_hits(disc.get("keyword_clusters", []),
                                   text_norm, threshold)
        auto_checked = hits >= threshold or disc.get("default_checked", False)
        results.append({
            "id":            disc["id"],
            "label":         disc["label"],
            "auto_checked":  auto_checked,
            "matched_count": hits,
        })

    # Sort: auto-checked first, then alphabetically
    results.sort(key=lambda d: (not d["auto_checked"], d["label"]))
    return results


def get_industry_label(industry_key: str) -> str:
    """Return the human-readable label for an industry key."""
    if industry_key in _INDUSTRIES:
        return _INDUSTRIES[industry_key].get("label", industry_key)
    return _FALLBACK.get("description", "General").split(".")[0]


def enrich_project(project: dict, full_doc_text: str) -> dict:
    """Attach industry + discipline detection results to a project dict.

    Adds:
        _industry          – industry key string
        _industry_label    – human-readable industry name
        _disciplines       – list of discipline dicts (with auto_checked)
    """
    industry_key = detect_industry(full_doc_text)
    disciplines  = detect_disciplines(industry_key, full_doc_text)

    project["_industry"]       = industry_key
    project["_industry_label"] = get_industry_label(industry_key)
    project["_disciplines"]    = disciplines
    return project


def all_industries() -> List[dict]:
    """Return the full list of industries for the UI dropdown."""
    result = [{"key": k, "label": v["label"]}
              for k, v in _INDUSTRIES.items()]
    result.append({"key": "fallback_general", "label": "Other / General"})
    return result
