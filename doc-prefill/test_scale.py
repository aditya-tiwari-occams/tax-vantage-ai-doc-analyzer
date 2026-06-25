"""
Scale + correctness test for the async pipeline.

Simulates the real workload (10 files, multiple projects each, ~30 pages worth
of content), then demonstrates:
  1. End-to-end run with bounded concurrency (extract -> segment -> prefill).
  2. Concurrency speedup: serial (1) vs parallel (N) LLM fan-out.
  3. Caching: a warm re-run makes ~zero LLM calls.

Runs fully offline using the mock LLM (set PREFILL_MOCK_LATENCY to mimic real
API latency). With OPENAI_API_KEY set it exercises the real AsyncOpenAI path.

    python3 test_scale.py
"""
import os, time, asyncio, tempfile, shutil
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

import pipeline, prefill


def fresh_cache():
    """Point the shared cache helpers at a brand-new empty dir."""
    d = tempfile.mkdtemp(prefix="scale_cache_")
    prefill.CACHE_DIR = d            # _cache_get/_cache_put read this at call time
    os.environ["POC_CACHE"] = d
    return d

N_FILES = 10
PROJECTS_PER_FILE = 3
LO = "\n".join(["%s" % s for s in [
    "Process of Experimentation: prototyped multiple approaches, evaluated against historical data, iterated on the design, and validated against field measurements.",
    "Technical Uncertainty: it was uncertain whether the chosen method would meet performance and regulatory thresholds under variable real-world conditions.",
    "Alternatives Considered: evaluated off-the-shelf options and conventional designs; rejected for cost, constructability, or performance reasons.",
]])


def make_doc(path, file_idx):
    doc = SimpleDocTemplate(path, pagesize=LETTER, topMargin=0.8*inch)
    ss = getSampleStyleSheet(); story = []
    story.append(Paragraph(f"R&D Tax Credit Study — Company {file_idx}", ss["Title"]))
    for j in range(PROJECTS_PER_FILE):
        story.append(Paragraph(f"Project Name: Job {file_idx*10+j} - Initiative {file_idx}.{j}", ss["Heading2"]))
        story.append(Paragraph("Business Component: New or improved system developed in 2025.", ss["BodyText"]))
        story.append(Paragraph(LO, ss["BodyText"]))
        story.append(Paragraph("Total Man Hours: %d   Employees on Research: %d   Supplies Used: compute, sensors, materials" % (1000+100*j, 3+j), ss["BodyText"]))
        story.append(Spacer(1, 10))
    doc.build(story)


async def main():
    workdir = tempfile.mkdtemp(prefix="scale_")
    paths = [os.path.join(workdir, f"company_{i}.pdf") for i in range(N_FILES)]
    for i, p in enumerate(paths):
        make_doc(p, i)
    print(f"Generated {N_FILES} PDFs x {PROJECTS_PER_FILE} projects "
          f"= {N_FILES*PROJECTS_PER_FILE} expected projects "
          f"(small files -> 1 LLM call each, model segments projects internally)\n")

    # realistic per-call latency so the concurrency effect is visible offline
    os.environ.setdefault("PREFILL_MOCK_LATENCY", "0.30")

    # ---- serial vs parallel (each with its OWN fresh empty cache) ----
    warm_dir = None
    for conc in (1, 8):
        warm_dir = fresh_cache()
        t = time.time()
        out = await pipeline.run_batch(paths, concurrency=conc)
        dt = time.time() - t
        tel = out["telemetry"]
        print(f"concurrency={conc:>2}  ->  {len(out['projects'])} projects  "
              f"| {tel['llm_calls']} llm_calls  | {dt:5.2f}s")

    # ---- warm cache re-run (reuse the conc=8 cache) ----
    t = time.time()
    out2 = await pipeline.run_batch(paths, concurrency=8)
    dt2 = time.time() - t
    tel2 = out2["telemetry"]
    print(f"\nwarm re-run     ->  {len(out2['projects'])} projects  "
          f"| {tel2['llm_calls']} llm_calls (expect ~0)  | {tel2['cache_hits']} cache_hits  | {dt2:5.2f}s")

    print("\nsample provenance:")
    for p in out2["projects"][:4]:
        print(f"  - {p['project_title'][:45]!r:48} {p['_source_file']} {p['_source_pages']}")

    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
