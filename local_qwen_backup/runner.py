runner.py
# =========================================================
# runner.py — Colab entry point for the Describer-Decider Engine
# Run this file in a Colab cell to execute the full pipeline.
# =========================================================

# Pin transformers==4.49.0 — versions >=4.50 break Qwen2.5-VL inference
# (Triton compilation errors + 15% accuracy regression on MMMU benchmark).
# See: github.com/QwenLM/Qwen3-VL/issues/1033, huggingface/transformers#41180
!pip install -q -U "transformers==4.49.0" accelerate huggingface_hub langchain langchain-core langgraph pydantic nest_asyncio "qwen-vl-utils==0.0.8" torchvision

import sys
import nest_asyncio
nest_asyncio.apply()

# Enable imports from the project directory on Google Drive
PROJECT_PATH = "/content/drive/MyDrive/Grad Project"
STEPS_PATH = f"{PROJECT_PATH}/steps"   # screenshots, logs, and steps.json live here
if PROJECT_PATH not in sys.path:
    sys.path.insert(0, PROJECT_PATH)

from models import load_project_data
from agents import load_models
from graph import build_graph

# Load models (mounts Drive, authenticates HF, loads Qwen models)
load_models()

# Build and run the analysis graph
app = build_graph()

print(f"🚀 DESCRIBER-DECIDER SCAN STARTED... {STEPS_PATH}")
try:
    steps_data = load_project_data(STEPS_PATH)
    if steps_data:
        res = app.invoke({
            "current_step_index": 0,
            "steps_data": steps_data,
            "trace_files_path": STEPS_PATH,
            "final_report": "",
            "stop_execution": False,
            "detected_errors": [],
            "active_failures": [],
            "active_failure_map": {},
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
        print("❌ CRITICAL: Could not load data files. Check paths.")
except Exception as e:
    print(f"FATAL SYSTEM CRASH: {e}")