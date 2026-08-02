# Old Version (v5.3) vs New System (v32.3) — Change Summary

---

## Quick Overview

| | Old Version (v5.3) | New System (v32.3) |
|---|---|---|
| **Files** | 1 monolithic file | 4 separate files (models, agents, graph, runner) |
| **AI Models** | Groq Cloud API — Llama 4 17B | Local GPU — Qwen2.5-VL-7B + Qwen2.5-14B |
| **Vision** | ❌ Model had `vision=False` | ✅ True Vision-Language Model |
| **Agents** | 1 agent doing everything | 3 specialized agents (Observer, Decider, Log Observer) |
| **Graph Nodes** | 1 node | 6 nodes |
| **Rate Limits** | `time.sleep(5)` every step | None — local models, no API |
| **Log Analysis** | ❌ Not supported | ✅ Full log pipeline |
| **Step Skipping** | ❌ Not implemented | ✅ Skips wait, non-VLM, null-target steps |
| **Decision Rules** | Loose, mode-based prompt | 4 explicit ordered rules |

---

## Change 1 — AI Model: Cloud API → Local GPU

**Old:**
```python
NEW_MODEL_NAME = "meta-llama/llama-4-maverick-17b-128e-instruct"
llm = ChatGroq(model=NEW_MODEL_NAME, temperature=0)
```
Used the **Groq cloud API** to call Llama 4. Every step sent screenshots to an external server. Required an API key, had rate limits, cost money per call, and had network latency.

**New:**
```python
_vision_model = Qwen2_5_VLForConditionalGeneration.from_pretrained("Qwen/Qwen2.5-VL-7B-Instruct", ...)
_logic_model  = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2.5-14B-Instruct", ...)
```
Both models run **locally on the A100 GPU**. No API key, no rate limits, no network, no cost per call. Models are downloaded once and cached on Google Drive.

---

## Change 2 — Vision Was Broken: Fixed with a Real Vision Model

**Old (critical bug):**
```python
groq_model_info = {
    "vision": False,   # ← The model could NOT see images
    ...
}
```
The old system encoded screenshots as base64 JPEG and sent them — but the model was declared as `vision=False`. The model was receiving the images but had no ability to interpret them. Visual analysis was essentially non-functional.

**New:**
Qwen2.5-VL-7B is a genuine **Vision-Language Model**. Images are converted to visual tokens that the model natively processes. The model truly "sees" both screenshots and describes UI elements, text content, and visual changes.

---

## Change 3 — Single Agent → Two Specialized Agents

**Old:**
One agent (Llama 4) received the screenshots + instruction and produced a pass/fail verdict directly. Observation and judgment were mixed in the same call.

**New:**
Two agents with strictly separated roles:

```
Vision Observer (Qwen2.5-VL-7B)    →    Logic Decider (Qwen2.5-14B)
"What do you SEE?"                       "Given what was seen, did it PASS?"
Outputs: text description                Inputs: text description (no images)
Never makes pass/fail judgments          Never sees images directly
```

**Why this matters:** The observer cannot be biased by the desire to pass or fail. The decider reasons from clean text evidence. Results are more reliable and auditable.

---

## Change 4 — AutoGen Reviewer: Removed, Replaced with Prompt Rules

**Old:**
```python
reviewer = AssistantAgent(
    name="Reviewer",
    model_client=autogen_client,
    system_message="You are a QA Auditor. Check if the Agent missed any numerical mismatches..."
)
# Called after every step:
review_reply = run_coro_sync(reviewer.run(task=review_prompt))
time.sleep(2)  # Extra wait for rate limits
```
An AutoGen agent was called after every step to double-check numerical values. However, this reviewer was also text-only (`vision=False`) so it could not actually see the screenshots to verify numbers.

**New:**
No separate reviewer agent. The Logic Decider has an explicit **Rule 2 — Strict Value Verification** built into its prompt:
```
RULE 2: If the instruction references a specific value (a time, a date, a price),
the Observer MUST have explicitly confirmed that exact value is visible in POST.
A UI transition alone is NOT sufficient. → FAILURE if value not confirmed.
```
One fewer LLM call per step, no extra rate limit delays, and the rule is actually enforceable because the Vision Observer's OCR is real.

---

## Change 5 — Rate Limiting Removed

**Old:**
```python
print("⏳ Waiting for rate limit cooldown (5s)...")
time.sleep(5)          # Before every step
time.sleep(2)          # Before reviewer call
time.sleep(10)         # On error
```
Every single step waited at least 7 seconds due to Groq API rate limits. For 25 steps, that's 3+ minutes of pure waiting.

**New:**
Zero `sleep()` calls. Local models have no rate limits. The only time cost is actual inference time.

---

## Change 6 — Graph: 1 Node → 6 Specialized Nodes

**Old:**
```
[analyst_node] → (continue/end) → [analyst_node] → ...
```
One massive `analyze_step_node()` function (~125 lines) handled everything: context extraction, image encoding, API call, reviewer call, cascading logic, error recording, and counter increment — all in one place.

**New:**
```
[router] → [skip]      (for non-VLM steps)
        → [vision]  → [log] → [decider] → [accumulate]
```

| Node | Single Responsibility |
|---|---|
| `router` | Extract step context, decide skip or analyze |
| `skip` | Increment counter for non-VLM steps |
| `vision` | Run Vision Observer (Qwen2.5-VL) |
| `log` | Run Log Observer (check for log files) |
| `decider` | Run Logic Decider (Qwen2.5-14B) |
| `accumulate` | Record result, update failure history, advance counter |

Each node is ~20 lines and does one thing. Easier to debug, test, and extend.

---

## Change 7 — Step Skipping: Not Implemented → Fully Handled

**Old:**
Every step went through the full pipeline. If a step was a `wait` action or had no images, it still tried to encode images and call the API.

**New:**
The router explicitly skips three types of steps before any image I/O:
- `action == "wait"` — pure time delay, nothing to observe
- `requiresVLM == false` — the test runner already verified this step
- `element == "null"` — no target element to look for

Also skips gracefully if screenshot files are missing, with a clear warning.

---

## Change 8 — WaitUntil Bug: Fixed

**Old:**
```python
target_element = current_step.get('element', 'Unknown')
```
For `waitUntil` steps, the target element is nested inside a `condition` sub-object. The old code always looked at the top level, so `waitUntil` steps always got `target = Unknown`.

**New:**
```python
condition = step_data.get('condition', {}) if action == 'waitUntil' else {}
target = step_data.get('element') or condition.get('element')
```
Explicitly drills into the `condition` object for `waitUntil` steps. The correct target element is extracted.

---

## Change 9 — Image Path: Array Index → step_idx Field

**Old:**
```python
prev_path = os.path.join(folder, f"step_{idx}_prev.png")
#                                          ^^^
#                               idx = array position (0,1,2,3...)
```
Used the array position in the steps list as the filename index.

**New:**
```python
step_num = ctx["step_num"]  # from step_idx field in JSON
prev_path = os.path.join(folder, f"step_{step_num}_prev.png")
#                                          ^^^^^^^^
#                               step_num = actual step_idx from JSON
```
Uses the `step_idx` field from the JSON, which is the true identifier. These can differ — for example, if step at array position 14 has `step_idx: 14` but step at position 3 has `step_idx: 3`. If any steps are non-sequential or reordered in the JSON, the old version would load the wrong screenshots.

---

## Change 10 — Decision Rules: Loose Mode-Switch → 4 Explicit Rules

**Old:**
```python
if is_verification:
    mode_instruction = "MODE: DATA VERIFICATION — Compare numbers/text..."
else:
    mode_instruction = "MODE: ACTION & NAVIGATION — Check if target exists..."
```
Two loose modes. No structured rules, no exceptions, no priority ordering. The model had to figure out edge cases itself.

**New:**
4 rules applied in strict priority order:

| Rule | Name | What It Handles |
|---|---|---|
| Rule 0 | System Overlay Exception | OS dialogs (notifications, permissions) are never failures |
| Rule 1 | Target Absent | Missing elements, with navigation & dismiss exceptions |
| Rule 2 | Strict Value Verification | Exact value confirmation required (times, prices, dates) |
| Rule 3 | Success | Default pass when no failure rule triggered |

Additionally: **two-phase prompt design** — verdict is decided from visual evidence first, then prior failure history is consulted only for root cause attribution. This prevents anchoring bias.

---

## Change 11 — Cascading Failure Handling

**Old:**
```python
final_res['failure_type'] = "cascading_failure"   # overwrites original type
final_res['root_cause'] += f" (Likely caused by Step {most_recent_fail})"
```
Changed the `failure_type` to `"cascading_failure"`, losing the original failure category.

**New:**
```python
# failure_type is KEPT as-is (element_missing / content_mismatch / etc.)
final_res.root_cause += f" (Likely caused by Step {most_recent_fail})"
```
Preserves the original failure type while still attributing the cascade. More informative — you know both *what* went wrong and *why* it likely happened.

---

## Change 12 — Log Analysis: Not Supported → Full Pipeline

**Old:** No log support. No code for reading or parsing system logs.

**New:** Full log pipeline:
- `LogObservation`, `LogEntry`, `NetworkEvent` Pydantic models
- `run_log_observer()` parses `step_N_logs.json` using the logic model
- Logic Decider receives structured log evidence and can upgrade PASS → FAIL for server errors invisible to the UI
- Zero overhead when no log files exist (early return, no LLM call)

---

## Change 13 — Black Screen Detection: Removed

**Old:**
```python
def is_black_screen(path: str) -> bool:
    img = Image.open(path).convert("L")
    arr = np.array(img)
    return float(arr.mean()) < 20.0 and float(arr.std()) < 5.0
```
Manual pixel-level check: if average brightness < 20 and standard deviation < 5, declare a critical visual bug.

**New:** Removed. The Vision Observer (a real VLM) naturally describes a black or empty screen in its observation text. The Logic Decider then classifies it as a failure. No hardcoded pixel thresholds needed.

---

## Change 14 — Modularization: 1 File → 4 Files

**Old:** ~340 lines, everything in one file. Setup, models, helpers, agents, graph, and execution all mixed together.

**New:**

| File | Responsibility | Lines |
|---|---|---|
| `models.py` | All Pydantic models, GraphState, helper functions | ~144 |
| `agents.py` | Model loading, Vision Observer, Logic Decider, Log Observer | ~446 |
| `graph.py` | All 6 LangGraph nodes, graph construction | ~239 |
| `runner.py` | Colab entry point, pip install, invocation | ~57 |

Each file can be read, understood, and modified independently. `runner.py` is the only file a user needs to run — it imports everything else.

---

## Summary Table of All Changes

| # | What Changed | Old | New |
|---|---|---|---|
| 1 | AI model source | Groq Cloud API | Local A100 GPU |
| 2 | Vision capability | Broken (`vision=False`) | Real VLM (Qwen2.5-VL) |
| 3 | Agent design | 1 agent (observe + decide) | 2 agents (observe / decide) |
| 4 | Reviewer agent | AutoGen (also text-only, no vision) | Removed → replaced with prompt rules |
| 5 | Rate limit waits | 5-10s sleep per step | Eliminated |
| 6 | Graph structure | 1 monolithic node | 6 specialized nodes |
| 7 | Step skipping | Not implemented | wait / requiresVLM=false / null target |
| 8 | waitUntil target | Broken (always "Unknown") | Fixed (drills into condition object) |
| 9 | Image file path | Array index | step_idx field from JSON |
| 10 | Decision logic | Loose mode switch | 4 explicit ordered rules |
| 11 | Cascading failure | Overwrites failure_type | Preserves type, appends cause |
| 12 | Log analysis | Not supported | Full LogObserver pipeline |
| 13 | Black screen check | Hardcoded pixel threshold | Handled naturally by VLM |
| 14 | Code structure | 1 monolithic file | 4 modular files |
