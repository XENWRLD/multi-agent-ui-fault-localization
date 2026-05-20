# =========================================================
# models.py — Pydantic models, GraphState, and helper functions
# Extracted from main.py v32.3 during Phase 1 modularization.
# =========================================================

import os
import json
import re
from typing import TypedDict, List, Dict, Any, Optional, Literal

from pydantic import BaseModel, Field


# =========================================================
# VISION & DECISION MODELS (unchanged from v32.3)
# =========================================================
class VisionObservation(BaseModel):
    prev_screen_elements: str = Field(default="Could not analyze PREV screen.")
    post_screen_changes: str = Field(default="Could not analyze POST screen.")
    semantic_screen_summary: str = Field(default="Could not determine screen state.")

class FinalDecision(BaseModel):
    expected_value: str = Field(default="N/A")
    observed_value: str = Field(default="N/A")
    is_failure: bool
    failure_type: str = Field(default="none")
    observation: str
    root_cause: str = Field(default="")


# =========================================================
# LOG MODELS (new — Phase 2 forward compatibility)
# =========================================================
class LogEntry(BaseModel):
    """A single parsed log event relevant to a test step."""
    timestamp: str = Field(default="")
    level: Literal["info", "warning", "error", "critical"] = Field(default="info")
    source: Literal["network", "console", "system", "application"] = Field(default="application")
    message: str = Field(default="")

class NetworkEvent(BaseModel):
    """A single HTTP request/response relevant to a test step."""
    method: str = Field(default="")
    url: str = Field(default="")
    status_code: int = Field(default=0)
    latency_ms: float = Field(default=0.0)
    error_message: str = Field(default="")

class LogObservation(BaseModel):
    """Structured observation from system and network logs for a single step."""
    has_logs: bool = Field(default=False)
    log_summary: str = Field(default="No logs available for this step.")
    relevant_errors: List[LogEntry] = Field(default_factory=list)
    network_events: List[NetworkEvent] = Field(default_factory=list)
    has_network_failure: bool = Field(default=False)
    has_system_error: bool = Field(default=False)
    latency_anomaly: bool = Field(default=False)


# =========================================================
# GRAPH STATE
# =========================================================
class GraphState(TypedDict):
    # Persistent state (accumulates across steps)
    current_step_index: int
    steps_data: List[dict]
    trace_files_path: str
    final_report: str
    stop_execution: bool
    detected_errors: List[dict]
    active_failures: List[str]
    active_failure_map: Dict[int, dict]
    # Transient per-step state (overwritten each iteration by nodes)
    _current_ctx: Optional[dict]
    _current_vision_obs: Optional[dict]
    _current_log_obs: Optional[dict]
    _current_decision: Optional[dict]
    _route: Optional[str]
    _skip_reason: Optional[str]


# =========================================================
# HELPER FUNCTIONS
# =========================================================
def load_project_data(folder_path: str) -> List[dict]:
    for filename in ["steps.json", "fail.json"]:
        path = os.path.join(folder_path, filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list): return data
                if isinstance(data, dict) and "stepDetails" in data: return data["stepDetails"]
    return []

def format_active_failures(failures: List[str]) -> str:
    return "\n".join(failures) if failures else "None. System is healthy so far."

def robust_json_extract(text: str) -> dict:
    # 1. Try markdown code block first (```json {...} ``` or ``` {...} ```)
    code_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if code_match:
        clean = code_match.group(1).replace("True", "true").replace("False", "false").replace("None", "null")
        try:
            return json.loads(clean)
        except Exception:
            pass

    # 2. Fallback: greedy match from first { to last }
    match = re.search(r'\{.*\}', text.strip(), re.DOTALL)
    if match:
        clean = match.group(0).replace("True", "true").replace("False", "false").replace("None", "null")
        try:
            return json.loads(clean)
        except Exception:
            pass

    return {}

def extract_step_context(raw_item: dict, current_idx: int) -> dict:
    step_data = raw_item.get('step', raw_item) if isinstance(raw_item, dict) else raw_item
    action = step_data.get('action', 'unknown')

    # Bug 2 fix: waitUntil steps nest the real element/instruction inside 'condition'.
    condition = step_data.get('condition', {}) if action == 'waitUntil' else {}

    instruction = (step_data.get('stepInstruction') or step_data.get('instruction')
                   or condition.get('stepInstruction') or condition.get('intent', 'None'))

    target = step_data.get('element')
    if not target or str(target).strip() == "" or str(target).strip().lower() == "null":
        target = condition.get('element') or condition.get('intent') if condition else None
    if not target or str(target).strip() == "":
        target = step_data.get('intent', 'General verification based on instruction')

    requires_vlm = step_data.get('requiresVLM', True)

    return {
        "step_num": step_data.get('step_idx', current_idx),
        "action": action,
        "instruction": instruction,
        "target": target,
        "requires_vlm": requires_vlm,
    }
