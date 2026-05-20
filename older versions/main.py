# =========================================================
# VERSION 32.3: STEP 18 FALSE POSITIVE — SURGICAL FIX
# Builds on v32.1 (Qwen2.5-VL baseline). Addresses 4 confirmed
# evaluation flaws: semantic mismatch, overlay interference,
# selection-state blindness, and loose value verification.
#
# Changes vs v32.1:
# 1. VisionObservation gains a new field: semantic_screen_summary.
#    The Observer now produces a 1-2 sentence high-level description
#    of the overall screen state after the action, giving the Decider
#    global context beyond element-level observations.
# 2. Vision Observer prompt overhauled:
#    - Explicit SEMANTIC EQUIVALENCE table (e.g. Çarpı = X icon).
#    - SELECTED STATE clause: selected/highlighted elements are present.
#    - OVERLAY DETECTION: system dialogs described + underlying screen noted.
#    - STRICT VALUE OCR: specific values must be confirmed visible; absence
#      must be stated explicitly.
#    - New Task 3: produce semantic_screen_summary.
#    - max_new_tokens raised 250 → 350 to accommodate the new field.
# 3. Logic Decider prompt overhauled:
#    - semantic_screen_summary injected into [OBSERVER REPORT].
#    - Rule 0 (NEW): SYSTEM OVERLAY EXCEPTION — system dialogs over a
#      successful state are never failures.
#    - Rule 1 revised: semantic equivalence reminder + NAVIGATION
#      EXCEPTION (target disappears because action navigated forward → SUCCESS).
#    - Rule 2 revised: if expected_value is a specific non-N/A value, the
#      observer must have explicitly confirmed it visible in POST. A UI
#      transition alone is insufficient verification → FAILURE.
#    - Rule 3 (SUCCESS) tightened accordingly.
# =========================================================

!pip install -q -U "bitsandbytes>=0.46.1" "transformers>=4.48.0" accelerate huggingface_hub langchain langchain-core langgraph pydantic nest_asyncio qwen-vl-utils torchvision

import os
import json
import torch
import gc
import re
import numpy as np
from typing import TypedDict, List, Dict, Any, Optional
from google.colab import drive
from PIL import Image
from huggingface_hub import login
import nest_asyncio

from pydantic import BaseModel, Field
from langchain_core.runnables import RunnableLambda
from langgraph.graph import StateGraph, END
from transformers import AutoProcessor, AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from transformers import Qwen2_5_VLForConditionalGeneration
from qwen_vl_utils import process_vision_info

nest_asyncio.apply()

# =========================================================
# 1. PYDANTIC MODELS & HELPERS
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
    match = re.search(r'\{.*\}', text.strip(), re.DOTALL)
    if match:
        clean_text = match.group(0).replace("True", "true").replace("False", "false").replace("None", "null")
        try:
            return json.loads(clean_text)
        except Exception as e:
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

# =========================================================
# 2. MODEL LOADING
# =========================================================
if not os.path.exists('/content/drive'):
    drive.mount('/content/drive')

PROJECT_PATH = "/content/drive/MyDrive/Grad Project"

if "HF_TOKEN" not in os.environ:
    os.environ["HF_TOKEN"] = input("Hugging Face Token: ")
login(token=os.environ["HF_TOKEN"])

print("\n📥 Loading Describer & Decider Models...")
bnb_config = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16)

try:
    vision_model = Qwen2_5_VLForConditionalGeneration.from_pretrained("Qwen/Qwen2.5-VL-7B-Instruct", quantization_config=bnb_config, device_map="auto", torch_dtype=torch.bfloat16)
    vision_processor = AutoProcessor.from_pretrained("Qwen/Qwen2.5-VL-7B-Instruct")

    logic_model_name = "Qwen/Qwen2.5-14B-Instruct"
    logic_model = AutoModelForCausalLM.from_pretrained(logic_model_name, quantization_config=bnb_config, device_map="auto")
    logic_tokenizer = AutoTokenizer.from_pretrained(logic_model_name)
    print("✅ Models Ready.")
except Exception as e:
    print(f"Error loading models: {e}")

# =========================================================
# 3. LANGCHAIN RUNNABLES (GÖZ VE BEYİN AJANLARI)
# =========================================================

# --- GÖZ AJANI ---
def run_vision_observer(inputs: dict) -> VisionObservation:
    ctx = inputs.get('ctx', {})
    raw_text_prompt = f"""You are an objective UI Observer. You DO NOT make pass/fail decisions.
Understand the instruction regardless of language, but output your observations in English.

[CONTEXT]
- ACTION TYPE: "{ctx.get('action', 'Unknown')}"
- INSTRUCTION: "{ctx.get('instruction', 'Unknown')}"
- TARGET ELEMENT: "{ctx.get('target', 'Unknown')}"

[OBSERVATION TASKS]

TASK 1 — TARGET SEARCH (PREV & POST):
Look at BOTH images. Is the target element visible?
- SEMANTIC EQUIVALENCE: The target may be named differently from its visual form.
  Match by meaning, not exact text:
  * "Çarpı", "kapat", "close", "dismiss" → an X icon or close button
  * "Geri", "back" → a left-pointing arrow or back chevron
  * Any city or airport name → that name written anywhere on screen
  * Any icon description → its corresponding symbol
  SCOPE: Semantic equivalence applies to UI control names and icon labels only.
  If the target contains a specific exact value — a time, date, number, or quoted
  string — match it character-by-character. Do NOT treat '21:15' as equivalent
  to '12:15'. A different value is not a semantic equivalent; it is a mismatch.
- SELECTED STATE: If the target element is selected, highlighted, tapped, or active
  in POST, it IS present. Report it as present and describe its active state explicitly.
- State clearly: target present in PREV only / POST only / Both / Neither.

TASK 2 — VISUAL CHANGES (POST vs PREV):
Describe what changed between PREV and POST.
- OVERLAY DETECTION: If a system dialog appeared (notification permission, location
  access, app rating, etc.), state that explicitly AND describe what is visible on the
  underlying screen behind it.
- STRICT VALUE OCR: If the instruction references a specific value (a time like
  '12.15', a city name, a package option), you MUST read the exact characters from
  POST. If the value is visible, quote it exactly. If it is NOT visible, state
  "VALUE NOT CONFIRMED IN POST".

TASK 3 — SEMANTIC SCREEN SUMMARY:
Write 1-2 sentences describing the overall screen state after the action.
Examples:
  "The departure city field now shows 'İzmir' as the selected value on the flight search page."
  "A system notification permission dialog is overlaying the main home screen after successful login."
  "The app navigated to the flight class selection screen after the time selection."

OUTPUT VALID JSON ONLY (no markdown, no extra keys):
{{
    "prev_screen_elements": "Target presence in PREV and key elements listed.",
    "post_screen_changes": "Target presence in POST, visual changes, overlay details, and exact OCR value or 'VALUE NOT CONFIRMED IN POST'.",
    "semantic_screen_summary": "1-2 sentence overall screen state description."
}}"""

    messages = [
        {"role": "user", "content": [
            {"type": "image", "image": inputs["prev"]},
            {"type": "image", "image": inputs["post"]},
            {"type": "text", "text": raw_text_prompt}
        ]}
    ]

    text = vision_processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs = process_vision_info(messages)

    model_inputs = vision_processor(text=[text], images=image_inputs, videos=video_inputs, padding=True, return_tensors="pt").to(vision_model.device)
    with torch.no_grad():
        generated_ids = vision_model.generate(**model_inputs, max_new_tokens=350, do_sample=False)
        generated_ids_trimmed = [out_ids[len(in_ids):] for in_ids, out_ids in zip(model_inputs.input_ids, generated_ids)]
        generated_text = vision_processor.batch_decode(generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]

    parsed_dict = robust_json_extract(generated_text)
    try:
        return VisionObservation(**parsed_dict)
    except:
        return VisionObservation(prev_screen_elements="Failed to parse", post_screen_changes="Failed to parse", semantic_screen_summary="Failed to parse")

# --- BEYİN AJANI ---
def run_logic_decider(inputs: dict) -> FinalDecision:
    ctx = inputs['ctx']
    vision_report = inputs['vision_report']
    active_failures = inputs.get('active_failures', [])

    # Bug 1 fix: Inject prior failures for root_cause enrichment ONLY.
    # We cap at the last 3 entries to limit noise, and we place this block
    # AFTER the decision rules in the prompt so the model commits to a
    # pass/fail verdict from visual evidence before it even reads this section.
    recent_failures = active_failures[-3:] if active_failures else []
    if recent_failures:
        failure_lines = "\n".join(f"  - {f}" for f in recent_failures)
        prior_context = (
            f"The following earlier steps have already failed:\n{failure_lines}\n"
            f"Use this ONLY to write root_cause if this step also fails. "
            f"Do NOT let this influence your is_failure verdict."
        )
    else:
        prior_context = "None. System is healthy so far."

    user_prompt = f"""You are the Lead QA Decider. Your job has two strict phases.

PHASE 1 — VERDICT: Read the Observer Report and apply the Decision Rules in order to set is_failure.
PHASE 2 — ROOT CAUSE: Only after you have committed to a verdict, consult the Prior Failure History to enrich root_cause.

[CONTEXT]
- ACTION TYPE: "{ctx['action']}"
- INSTRUCTION: "{ctx['instruction']}"
- TARGET ELEMENT: "{ctx['target']}"

[OBSERVER REPORT]
- PREV SCREEN: {vision_report.prev_screen_elements}
- POST SCREEN: {vision_report.post_screen_changes}
- SCREEN STATE SUMMARY: {vision_report.semantic_screen_summary}

[DECISION RULES — PHASE 1. Apply in order; stop at the first rule that matches.]

RULE 0 — SYSTEM OVERLAY EXCEPTION (highest priority):
If the SCREEN STATE SUMMARY indicates that a system-level dialog appeared over the main screen
(notification permission, location access, app rating, OS alert, etc.), the underlying action
is SUCCESSFUL regardless of what the overlay looks like.
System overlays are not application failures. → is_failure = false, failure_type = "none".

RULE 1 — TARGET ABSENT:
If the target element is absent from BOTH PREV and POST screens → FAILURE ('element_missing').
- Apply semantic equivalence: "Çarpı"/"kapat"/"close" = X icon; city names = any text
  of that name; icon descriptions = their visual symbol.
- CLOSE/DISMISS EXCEPTION: A close button, 'X', 'Çarpı', or dismiss control that is
  present in PREV but gone in POST → the modal was dismissed → SUCCESS.
- NAVIGATION EXCEPTION: If the target was present in PREV but is absent in POST AND the
  SCREEN STATE SUMMARY indicates the app navigated forward to the next logical screen
  (e.g., moved from search to results, from selection to class picker), the action
  completed successfully → SUCCESS.
  VALUE VERIFICATION OVERRIDE: If the instruction contains a specific non-N/A value
  to verify (a flight time, a date, a quoted string), this exception CANNOT grant
  SUCCESS on its own — Rule 2 must still be evaluated before a final verdict.

RULE 2 — STRICT VALUE VERIFICATION:
Extract expected_value from INSTRUCTION and observed_value from OBSERVER REPORT.
- If the instruction is general (no specific value to verify), set both to 'N/A' and skip.
- Punctuation-insensitive for times: 12.15 == 12:15 == 12,15.
- CRITICAL: If expected_value is a specific non-N/A value (a flight time, a city, a
  package name), the Observer MUST have explicitly confirmed it visible or selected in POST.
  A UI transition to the next screen is NOT sufficient. If the Observer reported
  "VALUE NOT CONFIRMED IN POST" or did not quote the exact value → FAILURE ('content_mismatch').
- If the confirmed observed_value differs fundamentally from expected_value → FAILURE ('content_mismatch').

RULE 3 — SUCCESS:
None of the above failure conditions matched. Target found (or navigation/overlay exception
applied), and value confirmed or N/A → is_failure = false, failure_type = "none".

[PRIOR FAILURE HISTORY — PHASE 2 only. Never use to decide is_failure.]
{prior_context}

OUTPUT VALID JSON ONLY:
{{
    "expected_value": "Specific value from INSTRUCTION, or 'N/A' if general.",
    "observed_value": "Exact value confirmed in POST by Observer, or 'N/A', or 'not confirmed'.",
    "is_failure": boolean,
    "failure_type": "element_missing" | "action_failed" | "content_mismatch" | "system_error" | "none",
    "observation": "Brief summary of what happened.",
    "root_cause": "If failed: direct cause + any relevant prior failure from history. If passed: empty string."
}}"""

    prompt_text = logic_tokenizer.apply_chat_template([{"role": "user", "content": user_prompt}], tokenize=False, add_generation_prompt=True)
    model_inputs = logic_tokenizer([prompt_text], return_tensors="pt").to(logic_model.device)

    with torch.no_grad():
        output = logic_model.generate(**model_inputs, max_new_tokens=200, do_sample=False)

    raw_text = logic_tokenizer.decode(output[0][len(model_inputs.input_ids[0]):], skip_special_tokens=True)
    parsed_dict = robust_json_extract(raw_text)

    if not parsed_dict:
        parsed_dict = {"is_failure": True, "failure_type": "system_error", "observation": "Logic agent parse failed.", "root_cause": raw_text}

    try:
        return FinalDecision(**parsed_dict)
    except:
        return FinalDecision(is_failure=True, observation="Parse fallback", root_cause="JSON parsing error")

vision_chain = RunnableLambda(run_vision_observer)
logic_chain = RunnableLambda(run_logic_decider)

# =========================================================
# 4. LANGGRAPH NODE & STATE
# =========================================================
class GraphState(TypedDict):
    current_step_index: int
    steps_data: List[dict]
    trace_files_path: str
    final_report: str
    stop_execution: bool
    detected_errors: List[dict]
    active_failures: List[str]
    active_failure_map: Dict[int, dict]

def analyze_step_node(state: GraphState):
    idx = state.get('current_step_index', 0)
    steps = state['steps_data']
    folder = state['trace_files_path']
    errors = list(state.get('detected_errors', []))
    active_failures = list(state.get('active_failures', []))
    active_failure_map = dict(state.get('active_failure_map', {}))

    if idx >= len(steps):
        report = f"\n{'='*40}\nTEST COMPLETE. Scanned {len(steps)} steps.\n{'='*40}\n"
        if errors:
            for err in errors: report += f"Step {err['step_index']}: {err['reason']}\n"
        else: report += "✅ ALL PASSED.\n"
        return {"stop_execution": True, "final_report": report}

    ctx = extract_step_context(steps[idx], idx)

    # Bug 3 fix: Skip steps that don't need VLM inference early, before any
    # image I/O. Covers: pure wait actions, requiresVLM=False flags from the
    # test runner, and legacy "null" string element values.
    is_wait_action = ctx["action"] == "wait"
    is_no_vlm_flag = not ctx.get("requires_vlm", True)
    is_null_target = str(ctx["target"]).strip().lower() == "null"
    if is_wait_action or is_no_vlm_flag or is_null_target:
        print(f"\n--- Step {idx} [SKIPPED — action={ctx['action']}, requires_vlm={ctx.get('requires_vlm', True)}] ---")
        return {"current_step_index": idx + 1, "detected_errors": errors,
                "active_failures": active_failures, "active_failure_map": active_failure_map}

    step_num = ctx["step_num"]

    prev_path = os.path.join(folder, f"step_{step_num}_prev.png")
    post_path = os.path.join(folder, f"step_{step_num}_post.png")

    print(f"\n--- Analyzing Step {idx} ---")

    if not os.path.exists(prev_path) or not os.path.exists(post_path):
        print(" ⚠️ Skipping: Images not found.")
        return {"current_step_index": idx + 1, "detected_errors": errors, "active_failures": active_failures, "active_failure_map": active_failure_map}

    try:
        vision_res = vision_chain.invoke({
            "ctx": ctx,
            "instruction": ctx["instruction"],
            "target": ctx["target"],
            "prev": prev_path, "post": post_path
        })

        final_res = logic_chain.invoke({
            "ctx": ctx,
            "vision_report": vision_res,
            "active_failures": active_failures,  # Bug 1 fix: pass history for root_cause attribution
        })

    except Exception as e:
        print(f" ⚠️ SYSTEM WARNING on Step {idx}: {e}")
        final_res = FinalDecision(is_failure=True, failure_type="system_error", observation="System crash", root_cause=str(e))

    gc.collect()
    if torch.cuda.is_available(): torch.cuda.empty_cache()

    if final_res.is_failure:
        if active_failure_map and final_res.failure_type == 'content_mismatch':
            most_recent_fail = max(active_failure_map.keys())
            # Only append the deterministic hint if the model didn't already
            # reference this step (avoids duplicating what the prompt produced).
            if f"Step {most_recent_fail}" not in final_res.root_cause:
                final_res.root_cause += f" (Likely caused by Step {most_recent_fail})"

        reason = final_res.root_cause if final_res.root_cause else final_res.observation
        print(f" 🚨 FAILED: {reason}")
        errors.append({"step_index": idx, "reason": reason})
        active_failures.append(f"Step {idx}: {reason}")
        active_failure_map[idx] = final_res.model_dump()
    else:
        print(f" ✅ PASSED: {final_res.observation}")

    return {"current_step_index": idx + 1, "detected_errors": errors, "active_failures": active_failures, "active_failure_map": active_failure_map}

# =========================================================
# 5. EXECUTION
# =========================================================
workflow = StateGraph(GraphState)
workflow.add_node("analyst", analyze_step_node)
workflow.set_entry_point("analyst")
workflow.add_conditional_edges("analyst", lambda s: "end" if s.get("stop_execution") else "continue", {"continue": "analyst", "end": END})
app = workflow.compile()

print(f"🚀 DESCRIBER-DECIDER SCAN STARTED... {PROJECT_PATH}")
try:
    steps_data = load_project_data(PROJECT_PATH)
    if steps_data:
        res = app.invoke({"current_step_index": 0, "steps_data": steps_data, "trace_files_path": PROJECT_PATH, "detected_errors": [], "active_failures": [], "active_failure_map": {}}, config={"recursion_limit": 150})
        if "final_report" in res: print(res["final_report"])
    else:
        print("❌ CRITICAL: Could not load data files. Check paths.")
except Exception as e:
    print(f"FATAL SYSTEM CRASH: {e}")