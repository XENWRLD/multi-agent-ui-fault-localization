# =========================================================
# runner.py — Colab entry point for the Describer-Decider Engine (v33.0)
# API-based: uses OpenAI GPT-4o. No GPU runtime required.
# =========================================================

!pip install -q openai langchain langchain-core "langgraph>=0.2,<0.3" pydantic nest_asyncio

import os
import sys
import traceback
import nest_asyncio
nest_asyncio.apply()

# Enable imports from the project directory and its parser/ sub-package on Google Drive
PROJECT_PATH = "/content/drive/MyDrive/Grad Project"
STEPS_PATH = f"{PROJECT_PATH}/steps"   # screenshots, logs, and steps.json live here
for _p in (PROJECT_PATH, os.path.join(PROJECT_PATH, "parser")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Set OpenAI API key (use Colab secrets or manual input)
if "OPENAI_API_KEY" not in os.environ:
    os.environ["OPENAI_API_KEY"] = input("OpenAI API Key: ")

# Mount Google Drive (for accessing step data)
from google.colab import drive
if not os.path.exists('/content/drive'):
    drive.mount('/content/drive')

from models import load_project_data
from graph import build_graph, set_log_source
from log_parser import parse_log_file

# Build and run the analysis graph
app = build_graph()

print(f"DESCRIBER-DECIDER SCAN STARTED... {STEPS_PATH}")
try:
    steps_data = load_project_data(STEPS_PATH)
    if steps_data:
        # Unified log ingestion: auto-detects format (raw unstepped, flat-indexed,
        # flat-assertions, step-nested) and assigns entries to steps accordingly.
        _parsed_logs = parse_log_file(os.path.join(STEPS_PATH, "log.json"), total_steps=len(steps_data))
        set_log_source(_parsed_logs)
        print(f"Log parser: format={_parsed_logs.format_name}, mode={_parsed_logs.mode}")
        if _parsed_logs.mode == "preassigned":
            print(f"  Pre-assigned entries across {len(_parsed_logs.step_map)} step bucket(s).")
        elif _parsed_logs.mode == "dynamic":
            print(f"  Dynamic assignment over {len(_parsed_logs.raw_entries)} raw entries.")
        for _w in _parsed_logs.warnings:
            print(f"  [WARN] {_w}")

        res = app.invoke({
            "current_step_index": 0,
            "steps_data": steps_data,
            "trace_files_path": STEPS_PATH,
            "final_report": "",
            "stop_execution": False,
            "detected_errors": [],
            "active_failures": [],
            "active_failure_map": {},  # str keys: str(step_idx) -> failure dict
            "step_results": [],
            # Transient per-step state (overwritten by nodes each iteration)
            "_current_ctx": None,
            "_current_vision_obs": None,
            "_current_log_obs": None,
            "_current_decision": None,
            "_route": None,
            "_skip_reason": None,
        }, config={"recursion_limit": 150})
        if "final_report" in res: print(res["final_report"])
    else:
        print("CRITICAL: Could not load data files. Check paths.")
except Exception as e:
    print(f"FATAL SYSTEM CRASH: {e}")
    traceback.print_exc()


# =========================================================
# COLAB FILE DOWNLOADER
# =========================================================
def download_results(results_dir=None):
    """Trigger browser download of output files (Colab only).

    Call this after app.invoke() completes to pull result files to your local machine.
    Safe outside Colab — prints a warning and exits gracefully.
    """
    try:
        from google.colab import files
    except ImportError:
        print("[download_results] Not running in Colab — skipping.")
        return
    if results_dir is None:
        results_dir = os.path.join(PROJECT_PATH, "results")
    if not os.path.isdir(results_dir):
        print(f"[download_results] Directory not found: {results_dir}")
        return
    downloaded = 0
    for fname in sorted(os.listdir(results_dir)):
        if fname.endswith(".txt") or fname.endswith(".json"):
            files.download(os.path.join(results_dir, fname))
            downloaded += 1
    print(f"[download_results] Triggered {downloaded} download(s).")
