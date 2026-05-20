# =========================================================
# graph.py — LangGraph multi-node conditional graph
# Phase 2: Decomposed from the single "analyst" node into
# router → skip/vision → log → decider → accumulate.
# Zero behavior change from v32.3 monolith.
# =========================================================

import os
import gc
import torch

from langgraph.graph import StateGraph, END

from models import GraphState, VisionObservation, FinalDecision, LogObservation, extract_step_context
from agents import vision_chain, logic_chain, log_chain


# =========================================================
# NODE 1: ROUTER — extract context, check end/skip conditions
# =========================================================
def router_node(state: GraphState):
    idx = state.get('current_step_index', 0)
    steps = state['steps_data']

    # End condition: all steps processed
    if idx >= len(steps):
        errors = list(state.get('detected_errors', []))
        report = f"\n{'='*40}\nTEST COMPLETE. Scanned {len(steps)} steps.\n{'='*40}\n"
        if errors:
            for err in errors: report += f"Step {err['step_index']}: {err['reason']}\n"
        else: report += "✅ ALL PASSED.\n"
        return {"stop_execution": True, "final_report": report}

    ctx = extract_step_context(steps[idx], idx)

    # Determine skip conditions (Bug 3 fix preserved)
    is_skip = (ctx["action"] == "wait"
               or not ctx.get("requires_vlm", True)
               or str(ctx["target"]).strip().lower() == "null")

    images_missing = False
    if not is_skip:
        folder = state['trace_files_path']
        step_num = ctx["step_num"]
        prev_path = os.path.join(folder, f"step_{step_num}_prev.png")
        post_path = os.path.join(folder, f"step_{step_num}_post.png")
        images_missing = not os.path.exists(prev_path) or not os.path.exists(post_path)

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

    prev_path = os.path.join(folder, f"step_{step_num}_prev.png")
    post_path = os.path.join(folder, f"step_{step_num}_post.png")

    print(f"\n--- Analyzing Step {idx} ---")

    try:
        vision_res = vision_chain.invoke({
            "ctx": ctx,
            "instruction": ctx["instruction"],
            "target": ctx["target"],
            "prev": prev_path, "post": post_path
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
# NODE 4: LOG — run log observer on per-step log file
# =========================================================
def log_node(state: GraphState):
    ctx = state.get('_current_ctx', {})
    folder = state['trace_files_path']
    step_num = ctx.get('step_num', 0)

    log_path = os.path.join(folder, f"step_{step_num}_logs.json")

    log_obs = log_chain.invoke({
        "ctx": ctx,
        "log_file_path": log_path,
    })
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
    except:
        vision_obs = VisionObservation()

    try:
        log_obs = LogObservation(**log_obs_dict) if log_obs_dict else LogObservation()
    except:
        log_obs = LogObservation()

    try:
        final_res = logic_chain.invoke({
            "ctx": ctx,
            "vision_report": vision_obs,
            "log_report": log_obs,
            "active_failures": active_failures,  # Bug 1 fix preserved
        })
    except Exception as e:
        print(f" ⚠️ SYSTEM WARNING on Step {idx}: {e}")
        final_res = FinalDecision(is_failure=True, failure_type="system_error", observation="System crash", root_cause=str(e))

    return {"_current_decision": final_res.model_dump()}


# =========================================================
# NODE 6: ACCUMULATE — record results, advance step counter
# =========================================================
def accumulate_node(state: GraphState):
    idx = state.get('current_step_index', 0)
    errors = list(state.get('detected_errors', []))
    active_failures = list(state.get('active_failures', []))
    active_failure_map = dict(state.get('active_failure_map', {}))

    decision_dict = state.get('_current_decision', {})
    try:
        final_res = FinalDecision(**decision_dict)
    except:
        final_res = FinalDecision(is_failure=True, observation="Decision parse fallback", root_cause="Accumulate error")

    gc.collect()
    if torch.cuda.is_available(): torch.cuda.empty_cache()

    if final_res.is_failure:
        if active_failure_map and final_res.failure_type == 'content_mismatch':
            most_recent_fail = max(active_failure_map.keys())
            if f"Step {most_recent_fail}" not in final_res.root_cause:
                final_res.root_cause += f" (Likely caused by Step {most_recent_fail})"

        reason = final_res.root_cause if final_res.root_cause else final_res.observation
        print(f" 🚨 FAILED: {reason}")
        errors.append({"step_index": idx, "reason": reason})
        active_failures.append(f"Step {idx}: {reason}")
        active_failure_map[idx] = final_res.model_dump()
    else:
        print(f" ✅ PASSED: {final_res.observation}")

    return {
        "current_step_index": idx + 1,
        "detected_errors": errors,
        "active_failures": active_failures,
        "active_failure_map": active_failure_map,
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

    # Analysis pipeline: vision → log → decider → accumulate
    workflow.add_edge("vision", "log")
    workflow.add_edge("log", "decider")
    workflow.add_edge("decider", "accumulate")

    # Loop back to router from both skip and accumulate
    workflow.add_edge("skip", "router")
    workflow.add_edge("accumulate", "router")

    return workflow.compile()
