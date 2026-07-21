# Stage 9 Field Mapping — from Occams Study Reports

How each Stage-9 form field is derived from the real study PDFs, based on the two
sample documents (`1-project.pdf`, `multi-project.pdf`). This is the contract the
LLM prompt (`prefill.py`, `SYSTEM_PROMPT`) encodes.

## Key finding: field availability varies by document
The two samples expose different data, so the extractor must gracefully leave
fields empty rather than guess.

| Data point | multi-project.pdf | 1-project.pdf |
|------------|-------------------|---------------|
| Per-project "Basic data" block | **Yes** (Man hours, Contract Type, Employee list) | No |
| Man hours | Yes (`Man hours: 24382`) | **Absent** → leave null |
| Employee names list | Yes | No |
| Costs as $ QREs / wages | Yes | Yes |
| Four-Part Test analysis | Yes | Yes (full PASS table) |

Rule: **never derive man-hours or headcount from dollar amounts.** If the report
states cost only in QRE/wage dollars, `total_man_hours` and
`employees_completing_research` stay null.

## Field-by-field mapping

| Stage 9 field | Source in the study report | Notes |
|---------------|----------------------------|-------|
| **Project Title** | `Project Name:` / `Project:` (e.g. "Job 314 - Coyote Flood Management") | Strip ID/year suffix |
| **Contract Type** | `Contract Type:` line | Map wording → enum: *Lump sum/fixed fee* → **Fixed Price**; *time and materials* → **Time & Material**; *cost plus* → **Cost Plus**; public-works/other → **Other**; explicit none → **Work was not done under Contract** |
| **Description** | "Project Description" / "Business Component" narrative | Summarize ≤ 1000 chars |
| **Technical Challenges / Uncertainties** | "Key Technical Uncertainty", Four-Part Test *Uncertainty* criterion, listed challenges | |
| **Solutions / Alternatives Considered** | "Process of Experimentation", approaches tried/rejected | |
| **Total Man Hours** | `Man hours: <n>` | Integer; **null** if absent |
| **Employees completing research** | Count of names after `Employees Completing Research:` / "conducted by the following employees" | Return the **count**, not the names; null if absent |
| **Supplies used** | "Supplies" line-items / materials actually used | Ignore statutory IRC definitions of "supplies" |
| **Tax Year** | "Tax Year(s): 2025" / "Project Years: 2024" | |
| **confidence** | Model's 0–1 self-rating; lower when fields left empty | Surfaced in UI for review |

## Verified extraction (offline mock, multi-project.pdf)
| Project | Contract | Man hours | Employees* |
|---------|----------|-----------|-----------|
| Job 314 — Coyote Flood Management | Fixed Price | 24,382 | ~170 |
| Job 317 — Tunitas Creek Beach Improvements | Fixed Price | 9,754 | ~74 |

\* Employee counts from the mock's name-list counting are approximate; the real
LLM counts distinct named individuals more precisely.

## Implications for the build
- The schema already makes `total_man_hours` and `employees_completing_research`
  nullable — correct, because some reports lack them.
- The UI should show empty AI-prefilled fields as "not found in document — please
  complete" rather than 0, so reviewers add the missing data.
- A low `confidence` on a project should visually flag it for closer review.
