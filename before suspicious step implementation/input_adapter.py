# =========================================================
# input_adapter.py — Format detection and normalization for
# Group 411 test case inputs alongside the existing v33 format.
#
# Responsibilities:
#   - detect_input_format()   — identify "g411" vs "v33"
#   - normalize_g411_steps()  — convert G411 stepDetails to canonical flat dicts
#   - find_fail_json()        — locate the trace file regardless of naming convention
#   - detect_mime_from_b64()  — infer image MIME type from raw base64 header
#
# No imports from project modules (avoids circular deps).
# =========================================================

import glob
import os
from typing import List, Literal

# G411 action values that don't exist in v33; mapped to their canonical equivalents
_G411_ACTION_MAP = {
    "verify": "abstractVerification",  # same semantics: confirm a UI state
    "type":   "click",                 # treated as click — vision confirms typed text appeared
}


def _map_action_g411(action: str) -> str:
    return _G411_ACTION_MAP.get(action, action)


def detect_input_format(data) -> Literal["v33", "g411"]:
    """Return 'g411' if data matches the Group 411 schema, else 'v33'.

    G411 fingerprint:
      - Top-level dict with a 'stepDetails' list
      - Each item has a nested 'step' sub-object (only action + stepInstruction)
      - Each item carries inline 'prevScreenshot' / 'currScreenshot' base64 strings
      - No 'step_idx' field anywhere in the step item
    """
    if not isinstance(data, dict):
        return "v33"
    steps = data.get("stepDetails")
    if not isinstance(steps, list) or not steps:
        return "v33"
    first = steps[0]
    if not isinstance(first, dict):
        return "v33"

    inner = first.get("step")
    if not isinstance(inner, dict):
        return "v33"

    # G411 inner step has a small, well-known key set with no step_idx
    inner_keys = set(inner.keys())
    has_g411_inner = inner_keys <= {"action", "stepInstruction", "condition"}
    has_inline_images = "prevScreenshot" in first and "currScreenshot" in first
    has_no_step_idx = "step_idx" not in inner and "step_idx" not in first

    if has_g411_inner and has_inline_images and has_no_step_idx:
        return "g411"
    return "v33"


def normalize_g411_steps(data: dict) -> List[dict]:
    """Convert Group 411 stepDetails into the canonical flat step format.

    Each output dict is compatible with extract_step_context() and graph nodes.
    Inline image base64 strings and log text are stored under private '_inline_*'
    keys so that vision_node and log_node can detect and use them without disk I/O.
    """
    result = []
    for idx, item in enumerate(data["stepDetails"]):
        inner = item["step"]
        action = _map_action_g411(inner.get("action", "click"))
        instruction = inner.get("stepInstruction", "")
        result.append({
            "step_idx":     idx,
            "action":       action,
            "stepInstruction": instruction,
            # No explicit element field in G411 — use instruction as the best proxy
            "element":      instruction,
            "requiresVLM":  True,
            # Inline image delivery (raw base64, no data: prefix)
            "_inline_prev_b64": item.get("prevScreenshot", ""),
            "_inline_curr_b64": item.get("currScreenshot", ""),
            # Inline log delivery (plain text strings, may be empty)
            "_inline_console_logs": item.get("consoleLogs", ""),
            "_inline_network_logs": item.get("networkLogs", ""),
        })
    return result


def detect_mime_from_b64(b64: str) -> str:
    """Infer image MIME type from the raw base64 header bytes.

    PNG magic: iVBORw0KGgo... (base64 of \\x89PNG)
    JPEG magic: /9j...        (base64 of FFD8)
    """
    if b64.startswith("/9j"):
        return "image/jpeg"
    return "image/png"


def find_fail_json(folder_path: str):
    """Locate the trace input file in folder_path.

    Search order:
      1. Exact names: steps.json, fail.json  (v33 convention)
      2. Glob *fail*.json                    (G411 naming: passo_fail.json, bank-app-fail.json, …)

    Returns the absolute path string, or None if nothing found.
    """
    for name in ("steps.json", "fail.json"):
        p = os.path.join(folder_path, name)
        if os.path.exists(p):
            return p
    matches = glob.glob(os.path.join(folder_path, "*fail*.json"))
    if matches:
        return matches[0]
    return None
