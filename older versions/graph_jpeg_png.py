# =========================================================
# graph.py — LangGraph multi-node conditional graph
# Phase 2: Decomposed from the single "analyst" node into
# router → skip/vision → log → decider → accumulate.
# Zero behavior change from v32.3 monolith.
# =========================================================

import os
import gc
import json

from langgraph.graph import StateGraph, END

from models import (
    GraphState, VisionObservation, FinalDecision, LogObservation,
    extract_step_context, find_likely_cause, build_failure_chains,
    build_diagnosis_report, extract_step_logs,
)
from agents import vision_chain, logic_chain, log_chain


# =========================================================
# IMAGE HELPERS — support png/jpg/jpeg
# =========================================================
def find_image(folder, step_num, suffix):
    """Find step image with supported extensions."""
    for ext in (".png", ".jpg", ".jpeg"):
        path = os.path.join(folder, f"step_{step_num}_{suffix}{ext}")
        if os.path.exists(path):
            return path
    return None


# =========================================================
# UNIFIED LOG STORE — module-level, never serialized by LangGraph
# =========================================================
_unified_logs: list = []

def set_unified_logs(logs: list) -> None:
    """Load log.json entries before invoking the graph. Avoids LangGraph state serialization."""
    global _unified_logs
    _unified_logs = logs if logs else []


# =========================================================
# NODE 1: ROUTER — extract context, check end/skip conditions
# =========================================================
def router_node(state: GraphState):
    idx = state.get('current_step_index', 0)
    steps = state['steps_data']

    # End condition: all steps processed
    if idx >= len(steps):
        errors = list(state.get('detected_errors', []))
        failure_map = dict(state.get('active_failure_map', {}))
        step_results = list(state.get('step_results', []))
        report = f"\n{'='*40}\nTEST COMPLETE. Scanned {len(steps)} steps.\n{'='*40}\n"
        report += build_diagnosis_report(
            step_results=step_results,
            detected_errors=errors,
            active_failure_map=failure_map,
            total_steps=len(steps),
        )
        return {"stop_execution": True, "final_report": report}

    ctx = extract_step_context(steps[idx], idx)

    # Determine skip conditions
    is_skip = (
        ctx["action"] == "wait"
        or not ctx.get("requires_vlm", True)
        or str(ctx["target"]).strip().lower() == "null"
    )

    images_missing = False
    if not is_skip:
        folder = state['trace_files_path']
        step_num = ctx["step_num"]
        prev_path = find_image(folder, step_num, "prev")
        post_path = find_image(folder, step_num, "post")
        images_missing = prev_path is None or post_path is None

    route = "skip" if (is_skip or images_missing) else "analyze"

    return {
        "_current_ctx": ctx,
        "_route": route,
        "_skip_reason": "images_missing" if images_missing else ("vlm_skip" if is_skip else ""),
    }


def route_step(state: GraphState) -> str:
    """Conditional edge: end / skip / analyze."""
    if state.get("stop_execution"):
        return "end"
    return state.get("_route", "analyze")


# =========================================================
# NODE 2: SKIP — increment counter for non-VLM steps
# =========================================================
def skip_node(state: GraphState):
    idx = state.get('current_step_index', 0)
    ctx = state.get('_current_ctx', {})
    reason = state.get('_skip_reason', '')

    if reason == "images_missing":
        print(f"\n--- Step {idx} ---")
        print(" ⚠️ Skipping: Images not found.")
    else:
        print(f"\n--- Step {idx} [SKIPPED — action={ctx.get('action', '?')}, requires_vlm={ctx.get('requires_vlm', True)}] ---")

    return {"current_step_index": idx + 1}


# =========================================================
# NODE 3: VISION — run vision observer on step images
# =========================================================
def vision_node(state: GraphState):
    ctx = state['_current_ctx']
    folder = state['trace_files_path']
    step_num = ctx['step_num']
    idx = state.get('current_step_index', 0)

    prev_path = find_image(folder, step_num, "prev")
    post_path = find_image(folder, step_num, "post")

    print(f"\n--- Analyzing Step {idx} ---")

    if prev_path is None or post_path is None:
        print(f" ⚠️ Missing images for step {step_num}")
        return {
            "_current_vision_obs": VisionObservation(
                prev_screen_elements="Missing image",
                post_screen_changes="Missing image",
                semantic_screen_summary="Images not found"
            ).model_dump()
        }

    try:
        vision_res = vision_chain.invoke({
            "ctx": ctx,
            "instruction": ctx["instruction"],
            "target": ctx["target"],
            "prev": prev_path,
            "post": post_path,
        })
    except Exception as e:
        print(f" ⚠️ Vision Observer error on Step {idx}: {e}")
        vision_res = VisionObservation(
            prev_screen_elements="System error",
            post_screen_changes="System error",
            semantic_screen_summary="Vision observer crashed"
        )

    return {"_current_vision_obs": vision_res.model_dump()}


# =========================================================
# NODE 4: LOG — run log observer (unified log.json or per-step file)
# =========================================================
def log_node(state: GraphState):
    ctx = state.get('_current_ctx', {})
    folder = state['trace_files_path']
    step_num = ctx.get('step_num', 0)
    total_steps = len(state.get('steps_data', []))

    try:
        if _unified_logs:
            # Unified mode: slice relevant entries from module-level log store
            relevant = extract_step_logs(ctx, _unified_logs, step_num, total_steps)
            if relevant:
                mode = "keyword" if len(relevant) >= 3 else "positional"
                print(f"   [LOG] Step {step_num}: {len(relevant)} entries ({mode} match)")
                log_obs = log_chain.invoke({
                    "ctx": ctx,
                    "log_text": json.dumps(relevant, ensure_ascii=False, default=str),
                })
            else:
                print(f"   [LOG] Step {step_num}: no log entries found")
                log_obs = LogObservation(has_logs=False)
        else:
            # Legacy mode: per-step file
            log_path = os.path.join(folder, f"step_{step_num}_logs.json")
            log_obs = log_chain.invoke({
                "ctx": ctx,
                "log_file_path": log_path,
            })

        if log_obs.has_logs:
            flags = []
            if log_obs.has_network_failure:
                flags.append("NET_FAIL")
            if log_obs.has_system_error:
                flags.append("SYS_ERR")
            if log_obs.latency_anomaly:
                flags.append("LATENCY")
            tag = f" [{', '.join(flags)}]" if flags else ""
            print(f"   [LOG] Observer: {log_obs.log_summary}{tag}")

    except Exception as e:
        print(f"   [LOG] ⚠️ Log observer failed on Step {step_num}: {e}")
        log_obs = LogObservation(has_logs=False)

    return {"_current_log_obs": log_obs.model_dump()}


# =========================================================
# NODE 5: DECIDER — run logic decider with all evidence
# =========================================================
def decider_node(state: GraphState):
    ctx = state['_current_ctx']
    vision_obs_dict = state.get('_current_vision_obs', {})
    log_obs_dict = state.get('_current_log_obs', {})
    active_failures = list(state.get('active_failures', []))
    idx = state.get('current_step_index', 0)

    try:
        vision_obs = VisionObservation(**vision_obs_dict)
    except Exception:
        vision_obs = VisionObservation()

    try:
        log_obs = LogObservation(**log_obs_dict) if log_obs_dict else LogObservation()
    except Exception:
        log_obs = LogObservation()

    try:
        final_res = logic_chain.invoke({
            "ctx": ctx,
            "vision_report": vision_obs,
            "log_report": log_obs,
            "active_failures": active_failures,
        })
    except Exception as e:
        print(f" ⚠️ SYSTEM WARNING on Step {idx}: {e}")
        final_res = FinalDecision(
            is_failure=True,
            failure_type="system_error",
            observation="System crash",
            root_cause=str(e)
        )

    return {"_current_decision": final_res.model_dump()}


# =========================================================
# NODE 6: ACCUMULATE — record results, advance step counter
# =========================================================
def accumulate_node(state: GraphState):
    idx = state.get('current_step_index', 0)
    ctx = state.get('_current_ctx', {})
    errors = list(state.get('detected_errors', []))
    active_failures = list(state.get('active_failures', []))
    active_failure_map = dict(state.get('active_failure_map', {}))
    step_results = list(state.get('step_results', []))

    decision_dict = state.get('_current_decision', {})
    try:
        final_res = FinalDecision(**decision_dict)
    except Exception:
        final_res = FinalDecision(
            is_failure=True,
            observation="Decision parse fallback",
            root_cause="Accumulate error"
        )

    # Post-hoc confidence adjustment based on evidence quality
    log_obs_dict = state.get('_current_log_obs', {})
    if log_obs_dict:
        log_summary = log_obs_dict.get('log_summary', '')
        if log_summary.startswith('Log parsing'):
            final_res.confidence = max(0.1, final_res.confidence - 0.10)
        elif not log_obs_dict.get('has_logs', False):
            final_res.confidence = max(0.1, final_res.confidence - 0.05)

    gc.collect()

    caused_by = None
    if final_res.is_failure:
        caused_by = find_likely_cause(
            current_idx=idx,
            current_failure_type=final_res.failure_type,
            current_ctx=ctx,
            active_failure_map=active_failure_map,
        )
        if caused_by is not None and f"Step {caused_by}" not in final_res.root_cause:
            final_res.root_cause += f" (Likely caused by Step {caused_by})"

        reason = final_res.root_cause if final_res.root_cause else final_res.observation
        print(f" FAILED: {reason}")
        if final_res.expected_behavior or final_res.observed_behavior:
            print(f"    Expected: {final_res.expected_behavior}")
            print(f"    Observed: {final_res.observed_behavior}")
        errors.append({
            "step_index": idx,
            "reason": reason,
            "expected_behavior": final_res.expected_behavior,
            "observed_behavior": final_res.observed_behavior,
            "expected_value": final_res.expected_value,
            "observed_value": final_res.observed_value,
            "failure_type": final_res.failure_type,
            "caused_by_step": caused_by,
        })
        active_failures.append(
            f"Step {idx} [type={final_res.failure_type}, element={ctx.get('target', '?')}]: {reason}"
        )
        active_failure_map[str(idx)] = {**final_res.model_dump(), "_ctx": ctx}
    else:
        print(f" PASSED: {final_res.observation}")

    step_results.append({
        "step_index": idx,
        "action": ctx.get("action", "unknown"),
        "target": str(ctx.get("target", "")),
        "instruction": str(ctx.get("instruction", "")),
        "verdict": "FAIL" if final_res.is_failure else "PASS",
        "confidence": final_res.confidence,
        "failure_type": final_res.failure_type,
        "observation": final_res.observation,
        "expected_behavior": final_res.expected_behavior,
        "observed_behavior": final_res.observed_behavior,
        "root_cause": final_res.root_cause,
        "caused_by_step": caused_by if final_res.is_failure else None,
    })

    return {
        "current_step_index": idx + 1,
        "detected_errors": errors,
        "active_failures": active_failures,
        "active_failure_map": active_failure_map,
        "step_results": step_results,
    }


# =========================================================
# GRAPH CONSTRUCTION
# =========================================================
def build_graph():
    """Build and compile the multi-node LangGraph StateGraph."""
    workflow = StateGraph(GraphState)

    # Register nodes
    workflow.add_node("router", router_node)
    workflow.add_node("skip", skip_node)
    workflow.add_node("vision", vision_node)
    workflow.add_node("log", log_node)
    workflow.add_node("decider", decider_node)
    workflow.add_node("accumulate", accumulate_node)

    # Entry point
    workflow.set_entry_point("router")

    # Router decides: end / skip / analyze
    workflow.add_conditional_edges("router", route_step, {
        "skip": "skip",
        "analyze": "vision",
        "end": END,
    })

    # Analysis pipeline
    workflow.add_edge("vision", "log")
    workflow.add_edge("log", "decider")
    workflow.add_edge("decider", "accumulate")

    # Loop back
    workflow.add_edge("skip", "router")
    workflow.add_edge("accumulate", "router")

    return workflow.compile()