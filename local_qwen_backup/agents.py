# =========================================================
# agents.py — Model loading and LangChain runnables
# Extracted from main.py v32.3 during Phase 1 modularization.
# =========================================================

import os
import torch
import gc

# ── Disable ALL three PyTorch JIT compilation systems ──
# 1. TorchDynamo (torch.compile / Inductor / Triton backend)
import torch._dynamo
torch._dynamo.config.disable = True

# 2. TorchScript profiling executor + NVFuser
torch._C._jit_set_profiling_executor(False)
torch._C._jit_set_profiling_mode(False)
try:
    torch._C._jit_set_nvfuser_enabled(False)
except AttributeError:
    pass  # removed in PyTorch 2.x — safe to skip

# 3. Legacy TorchScript fuser (fuser0/fuser1) — THIS is the one that
#    generates OffsetCalculator.cuh kernels and tries to compile them
#    via NVRTC, which fails on Colab due to missing libnvrtc-builtins.
torch._C._jit_override_can_fuse_on_gpu(False)
torch._C._jit_override_can_fuse_on_cpu(False)
# ── End JIT guards ──

from google.colab import drive
from huggingface_hub import login

from transformers import AutoProcessor, AutoModelForCausalLM, AutoTokenizer
from transformers import Qwen2_5_VLForConditionalGeneration
from qwen_vl_utils import process_vision_info
from langchain_core.runnables import RunnableLambda

from models import VisionObservation, FinalDecision, LogObservation, robust_json_extract


# =========================================================
# MODULE-LEVEL MODEL REFERENCES
# =========================================================
_vision_model = None
_vision_processor = None
_logic_model = None
_logic_tokenizer = None


# =========================================================
# MODEL LOADING
# =========================================================
def load_models():
    """Mount Drive, authenticate HuggingFace, and load both models."""
    global _vision_model, _vision_processor, _logic_model, _logic_tokenizer

    if not os.path.exists('/content/drive'):
        drive.mount('/content/drive')

    # Redirect HuggingFace cache to Drive — avoids filling Colab's local disk
    # (~42 GB of model weights) and persists weights across session reconnects.
    hf_cache = "/content/drive/MyDrive/hf_cache"
    os.makedirs(hf_cache, exist_ok=True)
    os.environ["HF_HOME"] = hf_cache

    if "HF_TOKEN" not in os.environ:
        os.environ["HF_TOKEN"] = input("Hugging Face Token: ")
    login(token=os.environ["HF_TOKEN"])

    print("\n📥 Loading Describer & Decider Models...")

    try:
        _vision_model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            "Qwen/Qwen2.5-VL-7B-Instruct",
            device_map="auto",
            torch_dtype=torch.bfloat16,
            attn_implementation="eager",
        )
        # Cap image resolution to control visual token count and VRAM usage.
        # Default max_pixels (1280*28*28 ≈ 1M) produces ~5120 tokens per image;
        # with 2 images + eager attention the O(N²) matrix blows past 20 GB.
        # 768*28*28 ≈ 602K pixels → ~3072 tokens/image → ~2.5 GB attention/layer.
        _vision_processor = AutoProcessor.from_pretrained(
            "Qwen/Qwen2.5-VL-7B-Instruct",
            min_pixels=256 * 28 * 28,   # 200,704  — keep default
            max_pixels=768 * 28 * 28,   # 602,112  — down from 1,003,520
        )

        # Fix generation config warnings: set no-op defaults so do_sample=False
        # doesn't clash with temperature/top_p/top_k from the model's config.
        _vision_model.generation_config.do_sample = False
        _vision_model.generation_config.temperature = 1.0
        _vision_model.generation_config.top_p = 1.0
        _vision_model.generation_config.top_k = 50

        logic_model_name = "Qwen/Qwen2.5-14B-Instruct"
        _logic_model = AutoModelForCausalLM.from_pretrained(
            logic_model_name,
            device_map="auto",
            torch_dtype=torch.bfloat16,
            attn_implementation="eager",
        )
        _logic_tokenizer = AutoTokenizer.from_pretrained(logic_model_name)

        _logic_model.generation_config.do_sample = False
        _logic_model.generation_config.temperature = 1.0
        _logic_model.generation_config.top_p = 1.0
        _logic_model.generation_config.top_k = 50

        print("✅ Models Ready.")
        if torch.cuda.is_available():
            alloc = torch.cuda.memory_allocated() / 1e9
            total = torch.cuda.get_device_properties(0).total_memory / 1e9
            print(f"   VRAM: {alloc:.1f} GB allocated / {total:.1f} GB total")
    except Exception as e:
        print(f"Error loading models: {e}")


# =========================================================
# GÖZ AJANI — Vision Observer
# =========================================================
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

    text = _vision_processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs = process_vision_info(messages)

    model_inputs = _vision_processor(text=[text], images=image_inputs, videos=video_inputs, padding=True, return_tensors="pt").to(_vision_model.device)

    # Reclaim any stale activation memory before the heaviest inference call
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    with torch.no_grad():
        generated_ids = _vision_model.generate(**model_inputs, max_new_tokens=350, do_sample=False)
        generated_ids_trimmed = [out_ids[len(in_ids):] for in_ids, out_ids in zip(model_inputs.input_ids, generated_ids)]
        generated_text = _vision_processor.batch_decode(generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]

    parsed_dict = robust_json_extract(generated_text)
    try:
        return VisionObservation(**parsed_dict)
    except:
        return VisionObservation(prev_screen_elements="Failed to parse", post_screen_changes="Failed to parse", semantic_screen_summary="Failed to parse")


# =========================================================
# BEYİN AJANI — Logic Decider
# =========================================================
def run_logic_decider(inputs: dict) -> FinalDecision:
    ctx = inputs['ctx']
    vision_report = inputs['vision_report']
    log_report = inputs.get('log_report')
    active_failures = inputs.get('active_failures', [])

    # If no LogObservation was passed, create a default no-logs instance
    if log_report is None:
        log_report = LogObservation()

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

    # Build log evidence section — ONLY when logs exist.
    # When has_logs=False, these strings are empty and the prompt is
    # byte-for-byte identical to v32.3 (zero regression).
    log_section = ""
    log_rules = ""
    if log_report.has_logs:
        log_section = f"""
[LOG EVIDENCE]
- LOG SUMMARY: {log_report.log_summary}
- NETWORK FAILURE DETECTED: {log_report.has_network_failure}
- SYSTEM ERROR DETECTED: {log_report.has_system_error}
- LATENCY ANOMALY: {log_report.latency_anomaly}"""
        if log_report.relevant_errors:
            log_section += "\n- ERROR ENTRIES:"
            for entry in log_report.relevant_errors[:5]:
                log_section += f"\n  [{entry.level.upper()}] [{entry.source}] {entry.message}"
        if log_report.network_events:
            log_section += "\n- NETWORK EVENTS:"
            for evt in log_report.network_events[:5]:
                log_section += f"\n  {evt.method} {evt.url} -> {evt.status_code} ({evt.latency_ms}ms) {evt.error_message}"

        log_rules = """
[LOG EVIDENCE RULES — apply AFTER visual decision rules, BEFORE Phase 2.]
- If visual verdict is PASS but NETWORK FAILURE DETECTED is true AND the failing request
  relates to the current action's target element or instruction, OVERRIDE to
  is_failure=true, failure_type="system_error". Cite the HTTP status in root_cause.
- If visual verdict is FAIL, check log evidence for explanatory context. Add any relevant
  HTTP errors, API details, or system errors to root_cause to explain WHY the failure occurred.
- A latency anomaly alone NEVER overrides a visual PASS verdict.
- Log evidence can ONLY override PASS → FAIL (for server errors invisible to the UI).
  Log evidence can NEVER override FAIL → PASS."""

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
{log_section}

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
{log_rules}

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

    prompt_text = _logic_tokenizer.apply_chat_template([
        {"role": "system", "content": "You are a JSON-only QA verdict engine. You MUST respond with a single raw JSON object. No markdown, no explanation, no text before or after the JSON."},
        {"role": "user", "content": user_prompt},
    ], tokenize=False, add_generation_prompt=True)
    model_inputs = _logic_tokenizer([prompt_text], return_tensors="pt").to(_logic_model.device)

    with torch.no_grad():
        output = _logic_model.generate(**model_inputs, max_new_tokens=200, do_sample=False)

    raw_text = _logic_tokenizer.decode(output[0][len(model_inputs.input_ids[0]):], skip_special_tokens=True)
    parsed_dict = robust_json_extract(raw_text)

    if not parsed_dict:
        parsed_dict = {"is_failure": True, "failure_type": "system_error", "observation": "Logic agent parse failed.", "root_cause": raw_text}

    try:
        return FinalDecision(**parsed_dict)
    except:
        return FinalDecision(is_failure=True, observation="Parse fallback", root_cause="JSON parsing error")


# =========================================================
# LOG OBSERVER — Parses per-step log files using logic model
# =========================================================
def run_log_observer(inputs: dict) -> LogObservation:
    """Parse system/network logs for a single step.

    Checks for step_N_logs.json in the trace folder. If no file exists,
    returns LogObservation(has_logs=False) immediately — no LLM call.
    If a file exists, feeds the raw log text to the logic model for
    structured extraction.

    Args:
        inputs: {
            'ctx': step context dict,
            'log_file_path': str — path to step_N_logs.json (may not exist),
        }
    """
    log_path = inputs.get('log_file_path', '')

    # Early return: no log file for this step
    if not log_path or not os.path.exists(log_path):
        return LogObservation(has_logs=False)

    # Read log file
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            log_text = f.read()
    except Exception:
        return LogObservation(has_logs=False)

    if not log_text.strip():
        return LogObservation(has_logs=False)

    # Use logic model (Qwen2.5-14B) to parse logs into structured form
    ctx = inputs.get('ctx', {})
    prompt = f"""You are a Log Analyst. Parse the following execution logs for a single test step.

[STEP CONTEXT]
- ACTION: "{ctx.get('action', 'Unknown')}"
- TARGET: "{ctx.get('target', 'Unknown')}"
- INSTRUCTION: "{ctx.get('instruction', 'Unknown')}"

[RAW LOGS]
{log_text[:3000]}

[TASK]
Analyze the logs above and produce a structured summary. Output valid JSON ONLY:
{{
    "log_summary": "1-3 sentence summary of what the logs reveal for this step.",
    "relevant_errors": [
        {{"timestamp": "...", "level": "error", "source": "network|console|system|application", "message": "..."}}
    ],
    "network_events": [
        {{"method": "GET|POST|...", "url": "...", "status_code": 200, "latency_ms": 150.0, "error_message": ""}}
    ],
    "has_network_failure": true/false,
    "has_system_error": true/false,
    "latency_anomaly": true/false
}}

RULES:
- has_network_failure = true if ANY request returned 4xx/5xx or timed out.
- has_system_error = true if ANY log entry at "error" or "critical" level exists.
- latency_anomaly = true if ANY request took longer than 5000ms.
- If no errors found, set relevant_errors to [] and booleans to false.
- Include at most 5 entries in relevant_errors and network_events."""

    prompt_text = _logic_tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}], tokenize=False, add_generation_prompt=True
    )
    model_inputs = _logic_tokenizer([prompt_text], return_tensors="pt").to(_logic_model.device)

    with torch.no_grad():
        output = _logic_model.generate(**model_inputs, max_new_tokens=300, do_sample=False)

    raw_text = _logic_tokenizer.decode(
        output[0][len(model_inputs.input_ids[0]):], skip_special_tokens=True
    )
    parsed = robust_json_extract(raw_text)

    if not parsed:
        return LogObservation(has_logs=True, log_summary="Log parsing failed — could not extract structured data.")

    parsed['has_logs'] = True
    try:
        return LogObservation(**parsed)
    except Exception:
        return LogObservation(has_logs=True, log_summary="Log parsing validation failed.")


# =========================================================
# LANGCHAIN RUNNABLE WRAPPERS
# =========================================================
vision_chain = RunnableLambda(run_vision_observer)
logic_chain = RunnableLambda(run_logic_decider)
log_chain = RunnableLambda(run_log_observer)
