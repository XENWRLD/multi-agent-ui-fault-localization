# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project Overview

This is an academic research project on **AI-powered fault localization** using Vision-Language Models (VLMs). The goal is to reduce the search space of root causes for failures in UI-driven applications (web/mobile) by analyzing sequences of screenshots alongside execution logs.

**The core hypothesis:** A VLM can observe UI state changes between a "before" and "after" screenshot, and a reasoning LLM can then determine whether the application behaved correctly given the intended action — producing structured, machine-readable fault reports.

The current implementation is the **Describer-Decider Engine v33.0**: a two-agent pipeline that separates *observation* (what happened visually?) from *judgment* (did it pass or fail, and why?). v33.0 replaces local Qwen models with the **OpenAI GPT-4o API**, adds cascading failure analysis (`find_likely_cause`, `build_failure_chains`, `build_diagnosis_report`), introduces `expected_behavior`/`observed_behavior`/`confidence` fields to `FinalDecision`, adds unified log slicing (`extract_step_logs`) from a single `log.json` file, and supports **Group 411 input format** (inline base64 images + logs) via `input_adapter.py`.

---

## Validated Performance (v33.0)

Evaluated on **12 apps / 136 steps** (7 proprietary + 5 Group 411 external cases):

| Dataset | Analyzed Steps | Precision | Recall | F1 |
|---|---|---|---|---|
| Our Traces (7 apps) | 85 | **100.0%** | **100.0%** | **100.0%** |
| Group 411 (5 cases) | 46 | 91.7% | 84.6% | 88.0% |
| **Combined** | **131** | **95.8%** | **92.0%** | **93.9%** |

Ground truth: 25 real failures across 12 apps. The 2 false negatives on Group 411 data are architectural gaps (cross-step state tracking, price-discount consistency), not systematic prompt failures. Full analysis in `documentation/Comparative_Analysis_Report_v33_Full.pdf`.

---

## Execution Environment

**Primary runtime: Google Colab.** All code must be compatible with Colab constraints:

- Begin notebooks with `!pip install -q ...` for all dependencies.
- Mount Google Drive at `/content/drive` for reading input data and writing results.
- `PROJECT_PATH` convention: `/content/drive/MyDrive/Grad Project`
- Apply `nest_asyncio.apply()` at startup to allow async in Colab's event loop.
- **No GPU required.** v33.0 is fully API-based (OpenAI). Any Colab runtime (CPU, T4, A100) works.
- **Auth**: Set `OPENAI_API_KEY` environment variable before running. `runner.py` prompts via `input()` if missing.

---

## Tech Stack (Locked Constraints)

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.10+ | — |
| Data validation | **Pydantic v2** | All agent I/O must be Pydantic models, never raw dicts |
| LLM framework | **LangChain** | Use `langchain-core` runnables for all agent steps |
| Agent orchestration | **LangGraph `>=0.2,<0.3`** | See rationale below |
| Vision model | **GPT-4o** (OpenAI API) | Replaces local Qwen2.5-VL-7B; no GPU required |
| Logic model | **GPT-4o** (OpenAI API) | Replaces local Qwen2.5-14B; no GPU required |
| Log observer | **GPT-4o-mini** (OpenAI API) | Cheaper model sufficient for log parsing |
| API client | **`openai` Python SDK** | Lazy-initialized; retry with exponential backoff via `_api_call_with_retry()` |
| Image encoding | `base64` (stdlib) | Images sent as inline `data:image/png;base64,...` or `data:image/jpeg;base64,...` in API messages |
| pip deps | `openai langchain langchain-core "langgraph>=0.2,<0.3" pydantic nest_asyncio` | No torch, transformers, or GPU packages required |

**Removed in v33.0**: `torch`, `transformers`, `bitsandbytes`, `qwen-vl-utils`, HuggingFace auth, JIT guards, `attn_implementation`, `torch_dtype`. None of these apply to the API-based implementation.

### Why LangGraph over AutoGen

LangGraph is the correct choice for this project because:
1. **Determinism and reproducibility** — academic research requires consistent, traceable execution paths. LangGraph's explicit graph with typed state is auditable.
2. **Sequential stateful processing** — fault localization is inherently ordered; each step's result feeds into a growing `active_failures` context for downstream steps.
3. **Conditional routing** — LangGraph's edge conditions are ideal for branching: skip non-VLM steps, short-circuit on critical failures, handle different action types (click vs. verify vs. wait) with different nodes.
4. **LangChain ecosystem fit** — already a hard constraint; LangGraph is LangChain's first-party orchestration layer.
5. **AutoGen** is better suited for open-ended multi-agent conversation loops and autonomous task discovery — unnecessary complexity for a well-defined sequential pipeline.

---

## Architecture

### Data Flow

```
steps.json (or *fail*.json) + images + log.json (optional)
        │
        │  [input_adapter.py]
        │  detect_input_format() → "v33" or "g411"
        │  normalize_g411_steps() if g411 (embeds inline b64 images + log text)
        │  find_fail_json() → resolves file path
        │
        ▼
  [extract_step_context]  ←── normalizes nested/flat step formats
        │
        ▼
  [Vision Observer Node]  ←── GPT-4o API (images as base64; disk path or inline _inline_*_b64)
  Inputs: prev image, post image, action context
  Output: VisionObservation (prev_screen_elements, post_screen_changes, semantic_screen_summary)
        │
        ▼
  [Log Observer Node]     ←── GPT-4o-mini API
  Inputs: ctx + sliced log entries (inline g411 logs / preassigned / dynamic / legacy file)
  Output: LogObservation (has_logs, log_summary, relevant_errors, network_events, flags)
        │
        ▼
  [Logic Decider Node]    ←── GPT-4o API (text-only)
  Inputs: VisionObservation + LogObservation + step context + active_failures history
  Output: FinalDecision (is_failure, failure_type, expected/observed value+behavior, root_cause, confidence)
        │
        ▼
  [Accumulate Node]       ←── LangGraph GraphState
  Post-hoc confidence adjustment (log parse failure, missing logs)
  Causal chain gating: find_likely_cause() only triggered when confidence ≥ 0.75 OR hard log evidence
  Tracks: detected_errors[], active_failures[], active_failure_map{}, step_results[]
        │
        ▼
  Final fault report: build_diagnosis_report() → failure chains + summary statistics
```

### LangGraph Graph Structure

**Current implementation (v33.0):** Multi-node conditional graph in `graph.py`:

```
entry → [router] ─── skip? ──→ [skip_node] ──────────────────────────────────┐
            │                                                                  │
            └── analyze ──→ [vision_node] → [log_node] → [decider_node] → [accumulate_node]
            │                                                                  │
            └── end? ──→ END                                         (back to router)
```

Nodes:
- `router_node` — context extraction (`extract_step_context`) + routing; generates final report via `build_diagnosis_report()` when all steps done
- `skip_node` — non-VLM steps (action=`wait`, `requiresVLM=False`, missing images, `element="null"`)
- `vision_node` — GPT-4o vision observer; sends prev+post images (disk path or inline G411 base64)
- `log_node` — priority order: (1) G411 inline console/network logs, (2) preassigned step_map lookup, (3) dynamic keyword/positional slicing, (4) legacy per-step file
- `decider_node` — GPT-4o logic decider; fuses vision + log evidence + prior failure history
- `accumulate_node` — records pass/fail, applies post-hoc confidence adjustment, runs `find_likely_cause()` for causal linking (gated by confidence ≥ 0.75 or hard log evidence), advances step counter

### Pydantic Models (defined in `models.py`)

All agent I/O is strictly typed:

```python
class VisionObservation(BaseModel):
    prev_screen_elements: str        # element-level observations on PREV screen
    post_screen_changes: str         # element-level observations on POST screen
    semantic_screen_summary: str     # 1-2 sentence high-level screen state after action

class FinalDecision(BaseModel):
    expected_value: str = "N/A"
    observed_value: str = "N/A"      # "NOT CONFIRMED" if value required but absent
    expected_behavior: str = ""      # What should have happened (derived from instruction)
    observed_behavior: str = ""      # What actually happened (derived from observer report)
    is_failure: bool
    failure_type: str = "none"       # "element_missing"|"action_failed"|"content_mismatch"|"system_error"|"none"
    observation: str
    root_cause: str = ""
    confidence: float = 0.5          # 0.0–1.0, self-assessed by Logic Decider

# Log models
class LogEntry(BaseModel):
    timestamp: str = ""; level: Literal["info","warning","error","critical"]; source: Literal["network","console","system","application"]; message: str

class NetworkEvent(BaseModel):
    method: str; url: str; status_code: int; latency_ms: float; error_message: str

class LogObservation(BaseModel):
    has_logs: bool = False
    log_summary: str = "No logs available for this step."
    relevant_errors: List[LogEntry] = []
    network_events: List[NetworkEvent] = []
    has_network_failure: bool = False
    has_system_error: bool = False
    latency_anomaly: bool = False
```

### GraphState (defined in `models.py`)

```python
class GraphState(TypedDict):
    current_step_index: int
    steps_data: List[dict]
    trace_files_path: str
    final_report: str
    stop_execution: bool
    detected_errors: List[dict]
    active_failures: List[str]
    active_failure_map: Dict[str, dict]      # str keys: str(step_idx) → failure dict
    step_results: List[dict]                 # per-step record for ALL analyzed steps (pass+fail)
    # Transient per-step state (overwritten each iteration)
    _current_ctx: Optional[dict]
    _current_vision_obs: Optional[dict]
    _current_log_obs: Optional[dict]
    _current_decision: Optional[dict]
    _route: Optional[str]
    _skip_reason: Optional[str]
```

### Helper Functions (defined in `models.py`)

- **`extract_step_context(raw_item, current_idx)`** — normalizes flat/nested step dicts; drills into `condition` for `waitUntil` steps.
- **`robust_json_extract(text)`** — extracts JSON from model output; tries markdown code block first, then greedy `{...}` match.
- **`_normalize_keywords(text)`** — strips Turkish chars, preserves time/decimal formats (e.g. `12.15`, `13:35`), splits on whitespace/arrows/punctuation but NOT on digit-flanked dots/colons. Returns lowercase set of 3+ char tokens.
- **`extract_step_logs(ctx, all_logs, step_idx, total_steps)`** — two-pass log slicing: keyword scoring (element_id/message/screen vs. target+instruction keywords; vlm_agent entries excluded) then positional window fallback when < 3 keyword matches.
- **`find_likely_cause(current_idx, current_failure_type, current_ctx, active_failure_map)`** — multi-factor scoring (element overlap + failure type correlation via `_TYPE_CORRELATION` matrix + temporal proximity decay). Requires element overlap > 0 AND total score > 0.3. Returns `None` if no plausible cause found.
- **`build_failure_chains(detected_errors, active_failure_map)`** — groups failures into causal chains using `caused_by_step` links. Output distinguishes ISOLATED FAILUREs from FAILURE CHAINs with root annotation.
- **`build_diagnosis_report(step_results, detected_errors, active_failure_map, total_steps)`** — assembles the full final report: Section A = failure chains, Section B = summary statistics (total/analyzed/skipped/passed/failed, confidence distribution by tier, failure type breakdown).
- **`load_project_data(folder_path)`** — calls `input_adapter.find_fail_json()`, then `detect_input_format()`. Returns normalized flat step list; handles G411 format (calls `normalize_g411_steps`), flat arrays, and `{"stepDetails": [...]}` wrappers.
- **`format_active_failures(failures)`** — formats active failure list for prompt injection.

### Input Adapter (`input_adapter.py`)

Handles format detection and normalization so the rest of the pipeline remains format-agnostic.

**Functions:**
- **`detect_input_format(data)`** — returns `"g411"` or `"v33"`. G411 fingerprint: top-level dict with `stepDetails`, each item has nested `step` sub-object (only `action`/`stepInstruction`/`condition`), carries `prevScreenshot`/`currScreenshot` base64 fields, no `step_idx`.
- **`normalize_g411_steps(data)`** — converts G411 `stepDetails` to canonical flat dicts. Inline images stored under `_inline_prev_b64`/`_inline_curr_b64`; inline logs under `_inline_console_logs`/`_inline_network_logs`. G411 action mapping: `"verify"` → `"abstractVerification"`, `"type"` → `"click"`.
- **`find_fail_json(folder_path)`** — search order: (1) `steps.json`, (2) `fail.json`, (3) glob `*fail*.json`. Returns absolute path or `None`.
- **`detect_mime_from_b64(b64)`** — infers MIME from base64 header: `"/9j..."` → `image/jpeg`, else → `image/png`. Used by `agents.py` for G411 inline images.

**G411 inline delivery**: When `_inline_prev_b64`/`_inline_curr_b64` are present in a step dict, `vision_node` uses them directly (no disk I/O). When `_inline_console_logs`/`_inline_network_logs` are present, `log_node` uses them with highest priority over any external log source.

### Unified Log Parser (`parser/log_parser.py`)

`parse_log_file(log_path, total_steps) -> ParsedLogs` ingests any supported `log.json` shape. Located in the `parser/` subdirectory; `runner.py` adds this to `sys.path`.

| mode | format_name values | How `log_node` uses it |
|---|---|---|
| `preassigned` | `flat_indexed`, `flat_assertions`, `nested` | `step_map.get(step_idx, [])` — ownership already known |
| `dynamic` | `raw`, `legacy_raw` | `extract_step_logs(ctx, raw_entries, ...)` — keyword+positional at query time |
| `empty` | `none`, `error` | falls back to per-step `step_N_logs.json` files (legacy) |

Supported input shapes (auto-detected via `_detect_format`):

1. **raw** — flat, unstepped, heterogeneous entries (baseline `steps/log.json`). No `step` field; step ownership inferred dynamically. vlm_agent source entries excluded from keyword matching.
2. **flat_indexed** — flat entries each carrying `step: <int>`. Shift condition: `min_step == 1` and `step 0` absent → shift all keys down by 1 (true 1-indexed signal). Warns on out-of-range residue after shift.
3. **flat_assertions** — flat entries with no `step` field; `source: "StepRunner"` + `message: "Step N passed/failed: ..."` markers define per-step ownership windows. Trailing entries orphaned with warning. Warns when marker count > steps.json steps (log from different test run).
4. **nested** — array where each item is `{step: {action, stepInstruction}, status, logs?: str}`. Array index **is** the `step_idx`. `logs` string parsed as pipe-delimited `key: value | ...` rows. Missing `logs` fields produce empty buckets with warning.

Installation in `graph.py`:

```python
_parsed_logs: ParsedLogs | None = None
set_log_source(parsed: ParsedLogs)     # canonical — call before app.invoke()
set_unified_logs(logs: list)           # legacy shim — wraps raw list into dynamic mode
```

`ParsedLogs.warnings` is surfaced by `runner.py` so index mismatches are visible at runtime rather than silently corrupting step assignment.

### steps.json / fail.json Schema

Steps may be flat (v33) or nested (G411). The canonical fields after normalization via `extract_step_context`:

| Field | Source | Meaning |
|---|---|---|
| `step_idx` | direct | Numeric step ID, maps to `step_N_prev.png` / `step_N_post.png` |
| `action` | direct | `click`, `waitUntil`, `abstractVerification`, `wait` (G411 `verify`→`abstractVerification`, `type`→`click`) |
| `element` | direct OR `condition.element` | Target UI element (for `waitUntil`, element is nested under `condition`) |
| `stepInstruction` | direct OR `condition.stepInstruction` | Human-readable intent |
| `requiresVLM` | optional, default `true` | If `false`, skip the vision pipeline |
| `useVLMCoordinates` | optional | Informational; step needed VLM for coordinate grounding |

**Critical**: `extract_step_context` must drill into `condition` for `waitUntil` steps to get the real `element` value. For G411 steps, `element` falls back to `stepInstruction` (no explicit element field).

---

## Agent Prompting Rules

### Vision Observer (GPT-4o) — max 500 tokens

- The observer **must not make pass/fail judgments** — it only reports what it sees.
- Receives **2 images** sent as base64 `image_url` content blocks: **PREV** then **POST**. `detail="high"` for accurate OCR.
- **TARGET STATE CHANGE**: If the target element changes visual state OR content between PREV and POST (color, size, enabled→disabled, wait time updated, badge count, status text changed), report it explicitly as `"Target state changed: [description]"`. A state-changed element is NOT absent — any update IS the response.
- **SCREEN CONTEXT CHANGE** (check first for visual changes): If POST shows a different screen title, navigation bar label, app header, or primary action button than PREV — even if overall similarity appears high — report as `"Screen context changed: [description]"`. Takes priority over content similarity.
- **NEW CONTENT SECTION** (check second): If POST shows new UI sections, lists, cards, form fields, price panels, or content blocks absent in PREV (e.g., category list expanding after a button click, bottom sheet appearing, form section revealing), report as `"New content appeared: [description]"`. Do NOT conclude "no change" when new content is visible.
- **SEMANTIC EQUIVALENCE**: Match target by meaning, not exact text. "Çarpı"/"kapat"/"close" → X icon; back controls in any language → left-pointing arrow; selectable item described by name → that name anywhere on screen; icon description → visual symbol. **Exception**: specific data values (times, numbers, quoted strings) must match character-by-character — `21:15` is NOT equivalent to `12:15`.
- **SELECTED STATE**: An element that is selected, highlighted, tapped, or active in POST **is present**. Report its active/selected state explicitly.
- **OVERLAY DETECTION**: If a system dialog (permission, OS alert, etc.) appears, describe it AND describe what is visible on the underlying screen behind it.
- **STRICT VALUE OCR**: Applies **only** to `abstractVerification`/`verify` action types AND the instruction asks to confirm a specific DATA VALUE (numeric, formatted string, quoted identifier). Does NOT apply to `click`, `waitUntil`, `type`, or `wait` steps — even when instruction contains "doğrula", "kontrol et", or "verify". **SCREEN PRESENCE EXCEPTION**: Instructions to "visually verify [screen name]", "confirm [element] is visible", or "check screen is displayed" are presence checks with no data value — do NOT write "VALUE NOT CONFIRMED IN POST". **STATIC VERIFICATION NOTE**: If PREV and POST appear visually identical on an `abstractVerification` presence check, this is expected and correct — the step only observes current state.
- **SEMANTIC SCREEN SUMMARY** (Task 3): 1-2 sentences describing the overall screen state after the action.
- Output: valid JSON only with exactly three fields: `prev_screen_elements`, `post_screen_changes`, `semantic_screen_summary`.

### Logic Decider (GPT-4o) — max 450 tokens

- Operates in two phases: **Phase 1 (verdict from visual + log evidence)** then **Phase 2 (root_cause enrichment from prior failure history)**.
- Must fill `expected_behavior` and `observed_behavior` for **every** step including passes.
- Must output `confidence` (0.0–1.0): 0.90–1.00 = all evidence strongly agrees; 0.70–0.89 = mostly agrees, minor ambiguity; 0.50–0.69 = notable ambiguity; 0.30–0.49 = high uncertainty. Use the full range — do NOT anchor to 0.9 for every step.
- Decision rules applied in strict priority order:

  **Rule 0 — SYSTEM OVERLAY EXCEPTION** (highest): system-level dialog over main screen → SUCCESS. System overlays are never application failures.

  **Rule 0B — APP LOADING EXCEPTION**: `click` action and POST shows a loading indicator overlay (spinner, progress bar, "loading" animation with NO interactive elements) absent in PREV → SUCCESS (click registered, app processing). Does NOT apply if the overlay contains buttons, lists, or selectable options — those are navigation outcomes evaluated under Rule 1.

  **Rule 0C — TARGET STATE CHANGE SUCCESS**: `click` action and Observer reports `"Target state changed: [description]"` (any visual or content change), OR target is selected/highlighted/tapped/active in POST but was NOT described as such in PREV → SUCCESS. **Takes priority over Rule 1.** A state-changed or content-updated element is NOT absent.

  **Rule 1 — TARGET ABSENT**: target missing from both PREV and POST → `element_missing`. Exceptions:
  - CLOSE/DISMISS EXCEPTION: any close/dismiss/cancel control present in PREV but gone in POST → SUCCESS (modal dismissed).
  - NAVIGATION EXCEPTION: target present in PREV but absent in POST AND `semantic_screen_summary` confirms forward navigation → SUCCESS. **Cannot grant SUCCESS if instruction requires value verification** (Rule 2 still applies).
  - WRONG-ITEM EXCEPTION (click steps only): target absent from PREV (never visible), but POST shows navigation or overlay appeared → classify as `content_mismatch`, NOT `element_missing`. `element_missing` is reserved for zero UI response.
  - WRONG-VALUE NAVIGATION: `click` on a specific formatted data value target (time range, price, date, numeric ID), and POST shows a detail view for a DIFFERENT value → `content_mismatch`. Navigation exception cannot grant SUCCESS when destination does not match target value.

  **Rule 2 — STRICT VALUE VERIFICATION**: applies only to `abstractVerification`/`verify` steps, OR steps with explicit verify/confirm directive naming a specific data value.
  - **STEP TYPE GATE**: `click`/`waitUntil`/`wait` skip Rule 2 unless instruction contains explicit verify directive AND names a specific DATA VALUE. "Click [item]", "Select [option]", "Tap [button]" are tap actions — go to Rule 3.
  - **WAIT-UNTIL PRESENCE CHECKS**: "wait until [element] appears", "verify [element] is present/visible" — presence check, no data value. `expected_value = 'N/A'`. If target visible in POST → Rule 3 (SUCCESS).
  - **SCREEN PRESENCE CHECKS**: instructions to "visually verify [screen name]" or "confirm [screen] is displayed" — `expected_value = 'N/A'`, skip to Rule 3.
  - **STATIC SCREEN NOTE**: visually identical PREV/POST on `abstractVerification` presence check → `expected_value = 'N/A'`, `is_failure = false` (target already in correct state).
  - When Rule 2 applies: Observer must have explicitly confirmed the data value; `"VALUE NOT CONFIRMED IN POST"` or mismatch → `content_mismatch`. Punctuation-insensitive for times (12.15 == 12:15).

  **Rule 3 — SUCCESS**: no failure condition matched.
  - **CONTENT EXPANSION SUCCESS**: Observer reports `"New content appeared"` (new sections, lists, cards, category selectors, or price panels in POST) → SUCCESS even if the trigger button is still visible.

- **Log evidence fusion** (conditional): When `LogObservation.has_logs=True`, `[LOG EVIDENCE]` and `[LOG EVIDENCE RULES]` are injected:
  - Logs can only override PASS→FAIL (server errors invisible to UI). Logs can NEVER override FAIL→PASS.
  - Latency anomaly alone never overrides a visual PASS.
  - **GESTURE-CONFIRMATION RULE**: A log entry that only records a user gesture ("User tapped X", "click registered") confirms input delivery — NOT that the app responded. For click/tap steps, gesture-only log is insufficient; POST must show visual state change. If POST is identical to PREV, treat gesture log as absent.
  - **WAIT-UNTIL LOG EXCEPTION**: If `action == "waitUntil"` and visual evidence shows target absent, but log explicitly confirms the wait condition was met ("element found", "wait condition satisfied", "step passed") → `is_failure = false`. A gesture-only log does NOT qualify.

- Prior failures (last 3 entries from `active_failures`) are injected **after** the decision rules to prevent anchoring bias on the is_failure verdict.

### Log Observer (GPT-4o-mini) — max 600 tokens

- Parses sliced log text and produces a `LogObservation`.
- Input normalized: uppercase `"INFO"`/`"ERROR"` replaced with lowercase before sending; `"DEBUG"` mapped to `"info"`.
- `_normalize_log_obs_dict()` coerces GPT-4o-mini output before Pydantic validation: normalizes level/source to valid Literal values, coerces `latency_ms` and `status_code` to correct numeric types.
- Field mapping from nested log objects: `method` ← `request.method`, `url` ← `request.endpoint`/`request.url`, `status_code` ← `response.status_code`, `latency_ms` ← `response.latency_ms`. Unknown sources (e.g. `vlm_evaluator`) mapped to `"application"`.
- Flags: `has_network_failure` (any 4xx/5xx or timeout), `has_system_error` (any error/critical entry), `latency_anomaly` (any request >5000ms).
- Returns max 5 entries each for `relevant_errors` and `network_events`.

### API Retry Behavior

- All API calls go through `_api_call_with_retry(call_fn, max_retries=3)` in `agents.py`.
- Retries on `OSError` (Colab socket errors: errno 107 ENOTCONN, 111 ECONNREFUSED) and transient API errors (rate limit, 502/503, timeout, connection).
- Re-creates the OpenAI client before each retry to clear stale sockets.
- Exponential backoff: 2s, 4s between retries.
- Non-retriable errors propagate immediately.

### Accumulate Node — Post-hoc Confidence Adjustment

After the Logic Decider's `confidence` is received, `accumulate_node` applies corrections the decider cannot know:
- Log parse failed (`log_summary` starts with "Log parsing") → `confidence -= 0.10`
- No log data at all (`has_logs=False`) → `confidence -= 0.05`
- Minimum confidence floor: `0.1`

**Causal chain gating**: `find_likely_cause()` is only called when `confidence >= 0.75` OR hard log evidence exists (`has_network_failure` or `has_system_error`). Low-confidence failures (mid-animation misreads, abstractVerification presence mis-fires) are recorded but cannot propagate spurious chains downstream.

---

## Known Issues & Improvement Roadmap

### Fixed in v32.0
1. ~~`active_failures` not injected~~ — **Fixed.** Two-phase prompt design.
2. ~~`waitUntil` element extraction broken~~ — **Fixed.** `extract_step_context` drills into `condition`.
3. ~~`wait` steps not skipped~~ — **Fixed.** Skip guard covers `action=="wait"`, `requiresVLM==False`, `element=="null"`.

### Fixed in v32.2
4. ~~Semantic mismatch~~ — **Fixed.** Equivalence table in Observer + Rule 1 semantic matching.
5. ~~Overlay interference~~ — **Fixed.** `semantic_screen_summary` + Rule 0 SYSTEM OVERLAY EXCEPTION.
6. ~~Selection-state blindness~~ — **Fixed.** SELECTED STATE clause in Observer.
7. ~~Loose value verification~~ — **Fixed.** Rule 2 requires explicit Observer confirmation; UI transition insufficient.

### Fixed in v33.0
8. ~~Local model dependency (Qwen + GPU)~~ — **Removed.** Fully API-based via OpenAI GPT-4o.
9. ~~No behavioral comparison~~ — **Fixed.** `expected_behavior`/`observed_behavior` in every `FinalDecision`.
10. ~~Flat failure list in report~~ — **Fixed.** `build_failure_chains()` + `build_diagnosis_report()` groups failures into chains + summary stats.
11. ~~No causal linking~~ — **Fixed.** `find_likely_cause()` with element overlap + type correlation + proximity scoring.
12. ~~Unified log.json~~ — **Added.** `extract_step_logs()` slices a single `log.json`; per-step files still supported as fallback.
13. ~~Rule 2 applied to click steps~~ — **Fixed.** STEP TYPE GATE in Rule 2 exempts plain click/tap steps.
14. ~~Log-step index mismatch FPs~~ — **Fixed.** `flat_indexed` parser correctly detects 1-indexed logs via min/max heuristic.
15. ~~No confidence scoring~~ — **Fixed.** `confidence` field in `FinalDecision`; post-hoc adjustment in `accumulate_node`; causal chain gating.
16. ~~G411 format incompatible~~ — **Fixed.** `input_adapter.py` detects and normalizes G411 stepDetails; inline images/logs delivered without disk I/O.
17. ~~No summary statistics in report~~ — **Fixed.** `build_diagnosis_report()` adds step counts, pass/fail rates, confidence distribution, failure type breakdown.

### Open Issues (from Comparative Analysis Report)

- **Cross-step state propagation** — Bank App FN (Step 6): balance unchanged after "successful" transfer is only detectable by comparing current balance against expected post-transfer value derived from earlier steps. Requires a `state_scratchpad` in `GraphState` to track key numerical values (balances, totals) from prior steps, injected as `[STATE CONTEXT]` for `abstractVerification` steps.
- **Price-discount inconsistency check** — Rent-a-Car FN (Step 8): when a promo code step's POST shows a discount success message alongside an unchanged total price, the Observer should cross-check whether the total reflects the claimed discount. Observer prompt lacks a clause for this trigger-reveal pattern.
- **Content-expansion FP pattern** — Passo FP (Step 5): Observer misread ticket category list appearance as button deactivation. Observer prompt needs explicit handling for trigger-reveal patterns where a button causes new content to appear rather than remaining visible post-click.
- **Missed causal chain (Yemeksepeti Step 7→14)** — `find_likely_cause()` scores below 0.3 due to 6-step temporal gap and click→abstractVerification type mismatch. Consider keyword similarity between failed `abstractVerification` instruction and prior failure `root_cause` strings to override the 0.3 threshold for this type pair.
- **Confidence miscalibration** — 3 incorrectly evaluated steps all had high confidence. Consider penalizing when `has_logs=False` for action types that typically generate backend events, or when `semantic_screen_summary` contradicts `post_screen_changes` at element level.

---

## Models in Use

| Role | Model | Notes |
|---|---|---|
| Vision Observer | **GPT-4o** | Images sent as inline base64; `detail="high"` for accurate OCR; max 500 tokens |
| Logic Decider | **GPT-4o** | Text-only; `response_format={"type":"json_object"}`; max 450 tokens |
| Log Observer | **GPT-4o-mini** | Cheaper; sufficient for log parsing; max 600 tokens |

All models use `temperature=0` for deterministic output.

---

## File & Folder Conventions

```
/content/drive/MyDrive/Grad Project/
├── models.py               # Pydantic models, GraphState, helper functions
│                           # (extract_step_logs, find_likely_cause, build_failure_chains,
│                           #  build_diagnosis_report, _normalize_keywords, ...)
├── agents.py               # OpenAI API agents (vision, logic, log observers) + RunnableLambda wrappers
│                           # (_normalize_log_obs_dict, _api_call_with_retry, _encode_image_base64)
├── graph.py                # LangGraph 6-node graph, _parsed_logs store (set_log_source / set_unified_logs)
│                           # find_image() supports .png/.jpg/.jpeg; _get_image_inputs() handles G411 inline b64
├── input_adapter.py        # Format detection and normalization (G411 ↔ v33)
│                           # (detect_input_format, normalize_g411_steps, find_fail_json, detect_mime_from_b64)
├── runner.py               # Colab entry point; adds both PROJECT_PATH and parser/ to sys.path
│                           # (pip install, auth, parse_log_file, set_log_source, app.invoke)
├── parser/
│   └── log_parser.py       # Unified log.json ingestion — parse_log_file() + ParsedLogs dataclass
│                           # (4 formats: raw, flat_indexed, flat_assertions, nested)
├── steps/
│   ├── steps.json          # Execution trace (action log); OR fail.json; OR *fail*.json
│   ├── step_N_prev.png     # Screenshot before step N (.png, .jpg, or .jpeg)
│   ├── step_N_post.png     # Screenshot after step N
│   ├── log.json            # (optional) Unified log file for all steps; preferred over per-step files
│   └── step_N_logs.json    # (optional, legacy) Per-step log entries; used when log.json absent
└── results/                # (to be created) fault reports output
```

- Images follow the naming pattern `step_{step_idx}_prev.{ext}` / `step_{step_idx}_post.{ext}` where `ext` is `.png`, `.jpg`, or `.jpeg`. `step_idx` matches the field in steps.json (not the array index).
- G411 inputs embed images inline as base64 strings in `prevScreenshot`/`currScreenshot` — no separate image files needed.
- **Import mechanism:** `runner.py` does `sys.path.insert(0, PROJECT_PATH)` AND `sys.path.insert(0, os.path.join(PROJECT_PATH, "parser"))` before importing `models`, `agents`, `graph`, and `log_parser`.
- **Log source API**: Call `set_log_source(parsed_logs)` (canonical) or `set_unified_logs(raw_list)` (legacy shim) in `runner.py` before `app.invoke()`. When not set, falls back to per-step `step_N_logs.json` files.
- **Recursion limit**: `app.invoke(..., config={"recursion_limit": 150})` — sufficient for datasets up to ~70 analyzed steps.
