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
    expected_behavior: str = Field(default="")
    observed_behavior: str = Field(default="")
    is_failure: bool
    failure_type: str = Field(default="none")
    observation: str
    root_cause: str = Field(default="")
    confidence: float = Field(default=0.5)   # 0.0–1.0, self-assessed by Logic Decider


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
    active_failure_map: Dict[str, dict]
    step_results: List[dict]             # per-step record for ALL analyzed steps (pass+fail)
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
    from input_adapter import detect_input_format, normalize_g411_steps, find_fail_json

    path = find_fail_json(folder_path)
    if not path:
        return []

    data = None
    for enc in ("utf-8", "utf-8-sig", "cp1254"):
        try:
            with open(path, "r", encoding=enc) as f:
                data = json.load(f)
            break
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    if data is None:
        return []

    fmt = detect_input_format(data)
    if fmt == "g411":
        print(f"[Adapter] Detected Group 411 format — normalizing {os.path.basename(path)}")
        return normalize_g411_steps(data)

    # v33 format (existing behavior)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "stepDetails" in data:
        return data["stepDetails"]
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

# =========================================================
# CASCADING FAILURE ANALYSIS
# =========================================================

# Failure type correlation matrix: (upstream_type, downstream_type) -> score
_TYPE_CORRELATION = {
    ("element_missing", "element_missing"): 1.0,
    ("element_missing", "content_mismatch"): 0.6,
    ("element_missing", "action_failed"): 0.8,
    ("element_missing", "system_error"): 0.3,
    ("content_mismatch", "element_missing"): 0.3,
    ("content_mismatch", "content_mismatch"): 1.0,
    ("content_mismatch", "action_failed"): 0.5,
    ("content_mismatch", "system_error"): 0.3,
    ("action_failed", "element_missing"): 0.8,
    ("action_failed", "content_mismatch"): 0.8,
    ("action_failed", "action_failed"): 0.9,
    ("action_failed", "system_error"): 0.5,
    ("system_error", "element_missing"): 0.5,
    ("system_error", "content_mismatch"): 0.5,
    ("system_error", "action_failed"): 0.5,
    ("system_error", "system_error"): 0.7,
}


def find_likely_cause(
    current_idx: int,
    current_failure_type: str,
    current_ctx: dict,
    active_failure_map: Dict[str, dict],
) -> Optional[int]:
    """Find the most likely upstream cause for the current failure.

    Uses three factors: element overlap, failure type correlation, and
    temporal proximity. Returns the step index of the likely cause,
    or None if no plausible cause is found.
    """
    if not active_failure_map:
        return None

    current_target = str(current_ctx.get("target", "")).lower()
    current_instruction = str(current_ctx.get("instruction", "")).lower()

    scores: Dict[int, float] = {}
    element_scores: Dict[int, float] = {}
    for key, fail_data in active_failure_map.items():
        fail_idx = int(key)  # Keys may be strings after LangGraph JSON round-trip
        fail_ctx = fail_data.get("_ctx", {})
        fail_target = str(fail_ctx.get("target", "")).lower()
        fail_instruction = str(fail_ctx.get("instruction", "")).lower()
        fail_type = fail_data.get("failure_type", "none")

        # Factor 1: Element overlap
        element_score = 0.0
        if (current_target or current_instruction) and (fail_target or fail_instruction):
            # Substring checks (high-confidence signals)
            if current_target and (current_target in fail_instruction or current_target in fail_target):
                element_score = 2.0
            elif fail_target and (fail_target in current_instruction or fail_target in current_target):
                element_score = 1.5
            else:
                # Keyword overlap — include both target AND instruction on both sides,
                # using _normalize_keywords for consistent tokenization (preserves time
                # formats, handles Turkish chars, splits on hyphens/arrows).
                current_words = _normalize_keywords(current_target) | _normalize_keywords(current_instruction)
                fail_words = _normalize_keywords(fail_target) | _normalize_keywords(fail_instruction)
                overlap = current_words & fail_words
                if overlap:
                    element_score = 1.0

        # Factor 2: Failure type correlation
        type_score = _TYPE_CORRELATION.get((fail_type, current_failure_type), 0.3)

        # Factor 3: Temporal proximity with decay
        distance = current_idx - fail_idx
        proximity_score = 1.0 / max(distance, 1)

        scores[fail_idx] = (element_score + type_score) * proximity_score
        element_scores[fail_idx] = element_score

    if not scores:
        return None

    best_idx = max(scores, key=scores.get)
    # Require element overlap — type correlation alone is not sufficient evidence
    if scores[best_idx] > 0.3 and element_scores[best_idx] > 0:
        return best_idx
    return None


def build_failure_chains(
    detected_errors: List[dict],
    active_failure_map: Dict[str, dict],
) -> str:
    """Build a chain-based failure report from detected errors.

    Groups failures into causal chains using the caused_by_step field
    stored during accumulation. Returns a formatted report string.
    """
    if not detected_errors:
        return "ALL PASSED."

    # Build adjacency: child -> parent (caused_by_step)
    parent_of = {}
    error_by_idx = {}
    for err in detected_errors:
        idx = err["step_index"]
        error_by_idx[idx] = err
        caused_by = err.get("caused_by_step")
        if caused_by is not None and caused_by in error_by_idx:
            parent_of[idx] = caused_by

    # Find chain roots (failures that are not caused by another failure)
    all_children = set(parent_of.keys())
    roots = [err["step_index"] for err in detected_errors if err["step_index"] not in all_children]

    # Build chains from roots
    children_of = {}
    for child, parent in parent_of.items():
        children_of.setdefault(parent, []).append(child)

    chains = []
    for root in roots:
        chain = []
        queue = [root]
        while queue:
            node = queue.pop(0)
            chain.append(node)
            queue.extend(sorted(children_of.get(node, [])))
        chains.append(chain)

    # Format report
    lines = []
    chain_num = 0
    for chain in chains:
        if len(chain) == 1:
            err = error_by_idx[chain[0]]
            lines.append(f"\nISOLATED FAILURE:")
            lines.append(f"  Step {chain[0]} [{err.get('failure_type', '?')}]: {err['reason']}")
            if err.get("expected_behavior"):
                lines.append(f"    Expected: {err['expected_behavior']}")
            if err.get("observed_behavior"):
                lines.append(f"    Observed: {err['observed_behavior']}")
        else:
            chain_num += 1
            chain_labels = " -> ".join(f"Step {s}" for s in chain)
            root_err = error_by_idx[chain[0]]
            lines.append(f"\nFAILURE CHAIN {chain_num}: ({len(chain)} steps)")
            lines.append(f"  {chain_labels}")
            lines.append(f"  Root: Step {chain[0]} [{root_err.get('failure_type', '?')}] - {root_err['reason']}")
            for step_idx in chain[1:]:
                err = error_by_idx[step_idx]
                caused_by = parent_of.get(step_idx, "?")
                lines.append(f"  Step {step_idx} [{err.get('failure_type', '?')}] - {err['reason']} (caused by Step {caused_by})")

    isolated = sum(1 for c in chains if len(c) == 1)
    multi = sum(1 for c in chains if len(c) > 1)
    summary_parts = []
    if multi:
        summary_parts.append(f"{multi} chain(s)")
    if isolated:
        summary_parts.append(f"{isolated} isolated")
    lines.append(f"\nSummary: {len(detected_errors)} failures across {', '.join(summary_parts)}")

    return "\n".join(lines)


def build_diagnosis_report(
    step_results: List[dict],
    detected_errors: List[dict],
    active_failure_map: Dict[str, dict],
    total_steps: int,
) -> str:
    """Assemble the full diagnosis report: failure chains and summary."""
    lines = []

    # ── Section A: Failure chain analysis ──
    lines.append("\nFAILURE CHAIN ANALYSIS")
    lines.append("=" * 22)
    lines.append(build_failure_chains(detected_errors, active_failure_map))

    # ── Section B: Summary statistics ──
    analyzed = len(step_results)
    skipped  = total_steps - analyzed
    passed   = sum(1 for r in step_results if r["verdict"] == "PASS")
    failed   = analyzed - passed
    pass_pct = (passed / analyzed * 100) if analyzed else 0.0
    fail_pct = (failed / analyzed * 100) if analyzed else 0.0

    high   = sum(1 for r in step_results if r["confidence"] >= 0.85)
    medium = sum(1 for r in step_results if 0.60 <= r["confidence"] < 0.85)
    low    = sum(1 for r in step_results if r["confidence"] < 0.60)
    avg_conf = (sum(r["confidence"] for r in step_results) / analyzed) if analyzed else 0.0

    type_counts: Dict[str, int] = {}
    for r in step_results:
        if r["verdict"] == "FAIL":
            ft = r["failure_type"]
            type_counts[ft] = type_counts.get(ft, 0) + 1

    lines.append("\n\nSUMMARY")
    lines.append("=" * 7)
    lines.append(f"Total steps:   {total_steps}")
    lines.append(f"  Analyzed:    {analyzed}")
    lines.append(f"  Skipped:     {skipped}")
    lines.append(f"  Passed:      {passed}   ({pass_pct:.1f}%)")
    lines.append(f"  Failed:      {failed}   ({fail_pct:.1f}%)")
    lines.append(f"\nConfidence Distribution (analyzed steps):")
    lines.append(f"  High   (>=0.85): {high:>2} steps")
    lines.append(f"  Medium (>=0.60): {medium:>2} steps")
    lines.append(f"  Low    (<0.60):  {low:>2} steps")
    lines.append(f"  Avg confidence: {avg_conf:.2f}")
    if type_counts:
        lines.append(f"\nFailure Type Breakdown:")
        for ft, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {ft:<22} {count}")

    return "\n".join(lines)


# =========================================================
# UNIFIED LOG SLICING
# =========================================================

def _normalize_keywords(text: str) -> set:
    """Strip Turkish chars and return tokens of 3+ chars as a lowercase set.

    Preserves decimal/time formats (e.g. "12.15", "13:35") so that flight-time
    targets survive tokenization and can match across steps. Splits on whitespace,
    arrows, commas, and brackets but NOT on dots/colons between digits.
    """
    replacements = {
        "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g", "ı": "i", "İ": "i",
        "ö": "o", "Ö": "o", "ş": "s", "Ş": "s", "ü": "u", "Ü": "u",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    # Split on whitespace, arrows, commas, slashes, brackets — but keep
    # dots and colons that sit between digits (time/decimal formats intact).
    tokens = re.split(r'\s+|->|-|[,;/\\()\[\]{}|!?@#$%^&*+=~`"\'<>]+', text)
    return {t.lower() for t in tokens if len(t) >= 3}


def extract_step_logs(
    ctx: dict,
    all_logs: List[dict],
    step_idx: int,
    total_steps: int,
    max_entries: int = 20,
) -> List[dict]:
    """Slice a unified log array to find entries relevant to a specific step.

    Two-pass strategy:
      Pass 1 — Keyword matching: score each entry against step target/instruction.
      Pass 2 — Positional window: fallback when keyword matches < 3.

    Args:
        ctx: Step context dict (target, instruction, action).
        all_logs: Full list of log entry dicts from log.json.
        step_idx: The step number (from steps.json step_idx).
        total_steps: Total number of steps in the run.
        max_entries: Maximum entries to return.

    Returns:
        List of matching log entry dicts (up to max_entries).
    """
    if not all_logs:
        return []

    target = str(ctx.get("target", ""))
    instruction = str(ctx.get("instruction", ""))
    keywords = _normalize_keywords(target) | _normalize_keywords(instruction)

    # Pre-filter: exclude vlm_agent entries (test-framework assertion records).
    # These entries describe what the *external test script* decided, not what
    # the app did. Keeping them causes keyword contamination across steps because
    # assertion messages repeat targets from verification steps (e.g., "BASIC",
    # "doğrula") and get matched to unrelated earlier steps.
    eligible_logs = [
        e for e in all_logs
        if str(e.get("source", "")).lower() != "vlm_agent"
    ]

    # Pass 1: keyword scoring
    scored = []
    for entry in eligible_logs:
        score = 0
        elem_id = str(entry.get("element_id", "")).lower()
        message = str(entry.get("message", "")).lower()
        screen = str(entry.get("screen", "")).lower()
        combined = f"{elem_id} {message} {screen}"

        for kw in keywords:
            if kw in combined:
                score += 2 if kw in elem_id else 1
        if score > 0:
            scored.append((score, entry))

    if len(scored) >= 3:
        # Sort by score descending, return top entries
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:max_entries]]

    # Pass 2: positional window fallback
    n = len(eligible_logs)
    start = int(step_idx / max(total_steps, 1) * n)
    end = min(start + max(n // max(total_steps, 1), 5), n)
    return eligible_logs[start:end][:max_entries]


# =========================================================
# STEP CONTEXT EXTRACTION
# =========================================================
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
