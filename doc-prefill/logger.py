"""
Per-run logging for the Stage-9 prefill pipeline.

Two outputs, so you can verify every step:
  1. TERMINAL  - timestamped step lines (extraction, segmentation, each LLM call,
                 results) printed to stdout, so you watch progress live.
  2. logs/<run_id>/  - a folder per request containing the FULL data:
        run.log                  human-readable step log
        01_extracted/<file>.txt  raw extracted text for each uploaded file
        02_segmentation.json     sections, strategy, token sizes, chunk counts
        03_llm/<n>_input.txt     exact text sent to the LLM for each call
        03_llm/<n>_output.json   exact JSON the LLM returned
        04_result.json           final aggregated projects + telemetry

Inspect logs/<run_id>/ after any run to confirm the steps worked.
"""
from __future__ import annotations
import os, json, time, logging, datetime, uuid

LOG_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# Console logger (shows up in the uvicorn terminal).
_console = logging.getLogger("prefill")
if not _console.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s", "%H:%M:%S"))
    _console.addHandler(h)
    _console.setLevel(logging.INFO)
    _console.propagate = False


class RunLogger:
    def __init__(self, label: str = "run"):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_id = f"{ts}_{uuid.uuid4().hex[:6]}"
        self.dir = os.path.join(LOG_ROOT, self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.t0 = time.time()
        self._lines: list[str] = []
        self.log(f"===== RUN {self.run_id} ({label}) =====")

    # ---- logging ----
    def log(self, msg: str, level: str = "info"):
        getattr(_console, level)(msg)
        self._lines.append(f"[{time.time() - self.t0:6.2f}s] {msg}")
        with open(os.path.join(self.dir, "run.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(self._lines) + "\n")

    def step(self, n, msg):           # e.g. logger.step("1/5", "Extracting ...")
        self.log(f"[{n}] {msg}")

    # ---- artifacts ----
    def save_text(self, relpath: str, text: str) -> str:
        p = os.path.join(self.dir, relpath)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def save_json(self, relpath: str, obj) -> str:
        p = os.path.join(self.dir, relpath)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, default=str, ensure_ascii=False)
        return p
