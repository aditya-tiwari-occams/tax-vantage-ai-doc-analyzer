"""End-to-end PoC runner.

Run twice in a row to see the caching kick in:
    python3 run_poc.py            # first pass: extract + LLM
    python3 run_poc.py            # second pass: pure cache hits, ~0ms, $0
"""
import os, json, sys
from prefill import prefill_from_file
from citations import ground_citations, render_batch_markdown

SAMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
FILES = [os.path.join(SAMP, f) for f in ("acme_two_projects.pdf", "beacon_one_project.docx")]


def banner(s): print("\n" + "=" * 70 + f"\n{s}\n" + "=" * 70)


def main():
    mode = "REAL OpenAI" if os.environ.get("OPENAI_API_KEY") else "MOCK (offline)"
    banner(f"Stage-9 document prefill PoC   |   LLM backend: {mode}")
    for path in FILES:
        result, ex, stats = prefill_from_file(path)
        print(f"\n--- {os.path.basename(path)} ---")
        print(f"extraction: method={ex.method} pages={ex.page_count} "
              f"chars={ex.char_count} needs_ocr={ex.needs_ocr}")
        print(f"cache: extract_hit={stats.extract_hit}  llm_hit={stats.llm_hit}  "
              f"llm_called={stats.llm_called}  elapsed={stats.elapsed_ms}ms")
        print(f"projects found: {len(result['projects'])}")
        for i, p in enumerate(result["projects"], 1):
            # Ground proof-of-work citations against the exact text the LLM
            # saw (prefill_from_file doesn't segment, so it's the whole doc).
            p["citations"] = ground_citations(p.get("citations"), ex.text,
                                              os.path.basename(path))
            print(f"  [{i}] {p['project_title']!r}")
            print(f"      contract_type = {p['contract_type']}")
            print(f"      man_hours = {p['total_man_hours']}  employees = {p['employees_completing_research']}")
            print(f"      supplies = {p['supplies_used']}")
            print(f"      uncertainty = {p['technical_challenges_uncertainties'][:90]}...")
            print(f"      alternatives = {p['solutions_alternatives_considered'][:90]}...")
            print(f"      confidence = {p['confidence']}")
            cited = [f for f, c in p["citations"].items() if c.get("quote")]
            verified = [f for f in cited if p["citations"][f].get("verified")]
            print(f"      proof of work: {len(verified)}/{len(cited)} citation(s) verified")
    # dump the full structured payload that the frontend would receive
    out = os.path.join(os.path.dirname(__file__), "last_prefill_output.json")
    payloads = {}
    for path in FILES:
        result, ex, _ = prefill_from_file(path)
        for p in result["projects"]:
            p["citations"] = ground_citations(p.get("citations"), ex.text,
                                              os.path.basename(path))
        payloads[os.path.basename(path)] = result
    with open(out, "w") as f:
        json.dump(payloads, f, indent=2)
    print(f"\nFull frontend payload written to {out}")

    # Human-readable "Proof of Work" report across all projects/files.
    all_projects = [p for r in payloads.values() for p in r["projects"]]
    md_out = os.path.join(os.path.dirname(__file__), "last_proof_of_work.md")
    with open(md_out, "w") as f:
        f.write(render_batch_markdown(all_projects))
    print(f"Proof-of-work report written to {md_out}")


if __name__ == "__main__":
    main()
