# =========================================================
# agents.py — OpenAI GPT-4o API agents (v33.0)
# Rewritten from local Qwen models to OpenAI API.
# Same RunnableLambda interface — graph.py is unchanged.
# =========================================================

import os
import base64
import json
import time

from openai import OpenAI
from langchain_core.runnables import RunnableLambda

from models import VisionObservation, FinalDecision, LogObservation, robust_json_extract
from input_adapter import detect_mime_from_b64


# =========================================================
# API CLIENT INITIALIZATION
# =========================================================
_client = None

def _get_client(force_new: bool = False) -> OpenAI:
    """Lazy-initialize the OpenAI client. Pass force_new=True to re-create after a socket error."""
    global _client
    if _client is None or force_new:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is not set.")
        _client = OpenAI(api_key=api_key)
    return _client


def _api_call_with_retry(call_fn, max_retries: int = 3):
    """Call call_fn() with retry + exponential backoff.

    Handles Colab-specific transport errors (errno 107 ENOTCONN) by
    re-creating the OpenAI client before each retry. Also handles
    transient rate limit and server errors from the API.
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            # Re-create client on any attempt after the first — clears stale sockets
            if attempt > 0:
                _get_client(force_new=True)
                wait = 2 ** attempt  # 2s, 4s
                print(f"  Retrying API call (attempt {attempt + 1}/{max_retries}) after {wait}s...")
                time.sleep(wait)
            return call_fn()
        except OSError as e:
            # errno 107 ENOTCONN, errno 111 ECONNREFUSED, etc. — Colab socket issues
            last_exc = e
            print(f"  Network error (errno {e.errno}): {e}. Will retry.")
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in ("rate limit", "503", "502", "timeout", "connection")):
                last_exc = e
                print(f"  API transient error: {e}. Will retry.")
            else:
                raise  # Non-retriable error — let it propagate
    raise RuntimeError(f"API call failed after {max_retries} attempts. Last error: {last_exc}")


def _encode_image_base64(image_path: str) -> str:
    """Read an image file and return its base64-encoded string."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _image_mime_type(image_path: str) -> str:
    """Return the correct MIME type for png/jpg/jpeg files."""
    ext = os.path.splitext(image_path)[1].lower()
    return "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"


# =========================================================
# GOZ AJANI — Vision Observer (GPT-4o)
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
- TARGET STATE CHANGE: If the target element IS visible in POST but in a DIFFERENT visual
  state OR CONTENT than in PREV — e.g., changed from active/colored/prominent to
  inactive/grayed/disabled, or from a large full-width button to a small secondary button,
  or from enabled to dimmed, OR if a text/numeric value WITHIN the element changed (e.g.,
  a wait time updated from '6 min' to '4 min', a badge count changed, a status label
  changed, a price updated) — this confirms the click was processed and the UI responded.
  Report it explicitly: "Target state changed: [describe the exact visual or content
  difference, e.g. 'button changed from large red active to small gray disabled', or
  'wait time updated from 6 min to 4 min inside the Lux ride option']."
  Do NOT treat a state-changed or content-updated element as "no change" — any update IS the response.
- SEMANTIC EQUIVALENCE: The target may be named differently from its visual form.
  Match by meaning, not exact text:
  * A close/dismiss control labeled in any language (e.g., an X symbol, a checkmark
    to confirm exit, or any word meaning "close", "cancel", or "dismiss") -> match it
    by its dismissal function, not by its exact label.
  * A back/navigation control labeled in any language -> a left-pointing arrow,
    chevron, or any visual that indicates return to the previous screen.
  * Any selectable item described by name (a product, a destination, a contact,
    an option) -> that item's name or label appearing anywhere on screen.
  * Any icon described by function -> its corresponding visual symbol.
  SCOPE: Semantic equivalence applies to UI control names and icon labels only.
  If the target contains a specific exact value — a number, date, code, or quoted
  string — match it character-by-character. Do NOT treat '21:15' as equivalent
  to '12:15', or '$49.99' as equivalent to '$59.99'. A different value is not a
  semantic equivalent; it is a mismatch.
- SELECTED STATE: If the target element is selected, highlighted, tapped, or active
  in POST, it IS present. Report it as present and describe its active state explicitly.
- State clearly: target present in PREV only / POST only / Both / Neither.

TASK 2 — VISUAL CHANGES (POST vs PREV):
Describe what changed between PREV and POST.
- SCREEN CONTEXT CHANGE (check this first): If POST shows a different screen title,
  navigation bar label, app header text, primary action button label, or top-level
  content section than PREV — even if overall visual similarity appears high — that
  constitutes a successful screen transition. Report it explicitly as:
  "Screen context changed: [describe the header/title/nav difference]."
  This takes priority over general content similarity. A new screen title, header,
  or app bar in POST means navigation succeeded — do NOT report "no significant change."
- NEW CONTENT SECTION (check second): If POST shows new UI sections, lists, cards,
  form fields, price panels, category selectors, or content blocks that were ABSENT
  in PREV — even when the navigation bar and background screen appear identical — the
  action produced a meaningful UI response. This includes: a category list expanding
  after a button click, a bottom sheet appearing, a form section revealing, a price
  panel loading. Report it explicitly as: "New content appeared: [describe the new
  sections/elements and how they differ from PREV]." Do NOT conclude "no change"
  when new content sections are visible in POST.
- OVERLAY DETECTION: If a system dialog appeared (notification permission, location
  access, app rating, etc.), state that explicitly AND describe what is visible on the
  underlying screen behind it.
- STRICT VALUE OCR: Applies ONLY when ACTION TYPE is "abstractVerification" or "verify" AND
  the instruction asks to confirm a specific DATA VALUE — a numeric value, formatted
  string, or quoted identifier (e.g., '42.50', '2024-03-15', 'REF-00123').
  Do NOT apply this to click, waitUntil, type, or wait steps — even when the
  instruction contains a verify word such as "doğrula", "kontrol et", or "verify".
  For all other steps (click, waitUntil, wait, type, and similar tap/navigation actions),
  confirming that the element is visible/selected/correct in POST is sufficient — do NOT
  write "VALUE NOT CONFIRMED IN POST" for them.
  When it does apply: read the exact characters from POST. If visible, quote it
  exactly. If not visible, state "VALUE NOT CONFIRMED IN POST".
  SCREEN PRESENCE EXCEPTION: If the instruction is "visually verify [screen name]",
  "confirm [element] is visible", "check screen is displayed", or any check that a
  screen or element IS showing with no specific numeric value, date, price, or quoted
  string to confirm — do NOT write "VALUE NOT CONFIRMED IN POST". These are screen
  presence checks, not data value checks. Describe what is visible on screen normally.
  STATIC VERIFICATION NOTE: If ACTION TYPE is "abstractVerification" or "verify" and PREV
  and POST appear visually identical, this is EXPECTED and CORRECT for a presence check
  where no user action is performed — the verification step only observes the current state.
  Do NOT write "VALUE NOT CONFIRMED IN POST" in this situation. Describe what is visible
  on screen as the confirmed screen state.

TASK 3 — SEMANTIC SCREEN SUMMARY:
Write 1-2 sentences describing the overall screen state after the action.
Examples:
  "The [field name] input now shows '[selected value]' as the confirmed selection on the [current screen]."
  "A system permission dialog is overlaying the main screen, requesting access to [resource]."
  "The app navigated to the [next screen name] after the [action] on the [previous screen]."

OUTPUT VALID JSON ONLY (no markdown, no extra keys):
{{
    "prev_screen_elements": "Target presence in PREV and key elements listed.",
    "post_screen_changes": "Target presence in POST, visual changes, overlay details, and exact OCR value or 'VALUE NOT CONFIRMED IN POST'.",
    "semantic_screen_summary": "1-2 sentence overall screen state description."
}}"""

    if inputs.get("prev_b64"):
        prev_b64 = inputs["prev_b64"]
        prev_mime = detect_mime_from_b64(prev_b64)
    else:
        prev_b64 = _encode_image_base64(inputs["prev"])
        prev_mime = _image_mime_type(inputs["prev"])

    if inputs.get("post_b64"):
        post_b64 = inputs["post_b64"]
        post_mime = detect_mime_from_b64(post_b64)
    else:
        post_b64 = _encode_image_base64(inputs["post"])
        post_mime = _image_mime_type(inputs["post"])

    messages = [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{prev_mime};base64,{prev_b64}", "detail": "high"}},
            {"type": "image_url", "image_url": {"url": f"data:{post_mime};base64,{post_b64}", "detail": "high"}},
            {"type": "text", "text": raw_text_prompt},
        ]}
    ]
    response = _api_call_with_retry(lambda: _get_client().chat.completions.create(
        model="gpt-4o", temperature=0, max_tokens=500,
        response_format={"type": "json_object"}, messages=messages,
    ))
    raw_text = response.choices[0].message.content
    parsed_dict = robust_json_extract(raw_text)

    try:
        return VisionObservation(**parsed_dict)
    except Exception:
        return VisionObservation(
            prev_screen_elements="Failed to parse",
            post_screen_changes="Failed to parse",
            semantic_screen_summary="Failed to parse",
        )


# =========================================================
# BEYIN AJANI — Logic Decider (GPT-4o)
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
- Log evidence can ONLY override PASS -> FAIL (for server errors invisible to the UI).
  Log evidence can NEVER override FAIL -> PASS.
- GESTURE-CONFIRMATION RULE: A log entry that only records a user gesture (e.g., "User tapped X",
  "click registered", "touch event", "User clicked X", "tapped [element]") confirms the INPUT
  was delivered to the device — it does NOT confirm the app responded or the screen changed.
  For click/tap/navigation steps, a gesture-only log is INSUFFICIENT to pass the step.
  The Observer's POST screen must ALSO show a visual state change (new screen, new content,
  selected state, or screen context change). If POST is visually identical to PREV, treat
  the gesture log as absent and apply Rule 1 based on visual evidence alone — the action
  had no observable effect on the UI, which is a failure regardless of what the log says.
- WAIT-UNTIL LOG EXCEPTION (waitUntil steps only): If ACTION TYPE is "waitUntil" and the
  visual evidence shows the target element as absent, but LOG EVIDENCE explicitly confirms the
  wait condition was met (e.g., "element found", "element visible during polling", "wait
  condition satisfied", "step passed") — the log evidence TAKES PRIORITY over the visual for
  this step type. -> is_failure = false, failure_type = "none".
  Rationale: for polling-based wait conditions, the test runner's detection result is
  temporally authoritative; a screenshot may be taken before or after the element's brief
  appearance. A gesture-only log does NOT qualify — the log must explicitly confirm the
  condition was met, not just that a check was initiated."""

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
System overlays are not application failures. -> is_failure = false, failure_type = "none".

APP LOADING EXCEPTION (also Rule 0):
If ACTION TYPE is "click" and POST shows a loading indicator overlay (circular spinner,
progress bar, or "loading" animation that contains NO interactive elements) that was absent
in PREV, the click was registered and the app is processing.
-> is_failure = false, failure_type = "none".
This exception applies ONLY when the overlay contains no interactive elements (no buttons,
lists, or selectable options). An overlay that contains interactive UI elements is a
navigation outcome and must be evaluated under Rule 1, not this exception.

RULE 0C — TARGET STATE CHANGE SUCCESS:
If ACTION TYPE is "click" and EITHER:
(a) The Observer explicitly reports "Target state changed: [description]" — covering any visual
    or content change: color, size, enabled/disabled, wait time updated, badge count changed,
    status text changed, or any text/numeric value within the element updated, OR
(b) The Observer reports the target element as selected, highlighted, tapped, or active in POST
    when it was NOT described as selected/active in PREV,
→ the click was registered and the UI responded.
-> is_failure = false, failure_type = "none".
This rule takes priority over Rule 1. A state-changed or content-updated element is NOT
"absent" — it is present with a different state or content, confirming the action was processed.

RULE 1 — TARGET ABSENT:
If the target element is absent from BOTH PREV and POST screens -> FAILURE ('element_missing').
- Apply semantic equivalence: a close/dismiss control in any language = its X icon or
  dismissal symbol; a selectable item described by name = that name anywhere on screen;
  icon descriptions = their visual symbol.
- CLOSE/DISMISS EXCEPTION: Any close button, dismiss icon, or cancel control (regardless
  of language or label) that is present in PREV but gone in POST -> the modal or overlay
  was dismissed -> SUCCESS.
- NAVIGATION EXCEPTION: If the target was present in PREV but is absent in POST AND the
  SCREEN STATE SUMMARY indicates the app navigated forward to the next logical screen
  (e.g., moved from search to results, from selection to class picker), the action
  completed successfully -> SUCCESS.
  VALUE VERIFICATION OVERRIDE: If the instruction contains a specific non-N/A value
  to verify (a specific data value, a date, a quoted string), this exception CANNOT grant
  SUCCESS on its own — Rule 2 must still be evaluated before a final verdict.
- WRONG-ITEM EXCEPTION (click steps only): If ACTION TYPE is 'click' and the target was
  absent from PREV (never visible in the list/screen at all), but POST shows the app
  navigated forward or an overlay appeared (i.e., something was clicked — just not the
  intended item), do NOT classify as 'element_missing'. Classify as 'content_mismatch'
  instead. 'element_missing' is reserved for cases where NO action occurred at all
  (no screen change, no navigation, no overlay).
- WRONG-VALUE NAVIGATION (click steps on data-value targets): If ACTION TYPE is 'click'
  AND the TARGET ELEMENT is a specific formatted data value (a time range, a price, a date,
  or a numeric/code identifier — not a generic button label or control name), AND POST shows
  a DETAIL VIEW or SELECTION OVERLAY whose content refers to a DIFFERENT value than the target
  (e.g., the target was a specific item identifier but the overlay or detail screen shows a
  different identifier), the click landed on the WRONG item. Classify as 'content_mismatch'.
  The Navigation Exception cannot grant SUCCESS when the navigation destination does not match
  the target value.

RULE 2 — STRICT VALUE VERIFICATION:
This rule applies ONLY to abstractVerification steps, OR to steps where the instruction
explicitly asks to VERIFY/CONFIRM a specific data value is displayed (not just "tap X").

STEP TYPE GATE — check ACTION TYPE first:
- If ACTION TYPE is "click", "waitUntil", or "wait": Rule 2 applies ONLY if the instruction
  contains an explicit verify/confirm directive AND names a specific DATA VALUE (a numeric value,
  a price, a date, or a quoted identifier). "Click [item name]", "Select [option]", "Tap [button]"
  are tap actions — they do NOT trigger Rule 2. Go straight to Rule 3 for these.
  WAIT-UNTIL PRESENCE CHECKS: For "waitUntil" steps where the instruction says "wait until
  [element] appears", "wait until [screen] loads", "verify [element] is present/visible", or
  any check that a screen or element IS showing — these are PRESENCE CHECKS with no data value
  to confirm. expected_value = 'N/A'. If the target element is visible in POST, go to Rule 3
  (SUCCESS). Do NOT apply Rule 2 strict value matching for these steps.
- If ACTION TYPE is "abstractVerification" or "verify": Rule 2 always applies.

When Rule 2 applies:
- expected_value = the specific DATA VALUE named in the instruction (e.g., "12:15", "21", "$150").
  City names, button labels, and field names in click steps are TAP TARGETS, not data values.
  SCREEN PRESENCE CHECKS: Instructions that ask to "visually verify [screen name]", "confirm
  [screen/element] is visible", or "check that [screen] is displayed" contain NO specific data
  value — even for abstractVerification steps. These are screen state checks, not value checks.
  -> expected_value = 'N/A', skip to Rule 3 (SUCCESS if the target screen/element is visible).
  If no specific data value exists -> set both to 'N/A' and skip to Rule 3.
  STATIC SCREEN NOTE: If PREV and POST appear visually identical AND ACTION TYPE is
  "abstractVerification" AND the instruction is a presence check (verify screen/element is
  visible, no action expected, no data value named), identical screens confirm the target was
  already in the correct state and the observation verified it. -> expected_value = 'N/A',
  is_failure = false (Rule 3 SUCCESS). Do NOT apply Rule 2 strict matching here.
- Punctuation-insensitive for times: 12.15 == 12:15 == 12,15.
- CRITICAL: The Observer MUST have explicitly confirmed the data value visible in POST.
  A UI transition alone is NOT sufficient. If Observer reported "VALUE NOT CONFIRMED IN POST"
  or did not quote the exact value -> FAILURE ('content_mismatch').
- If observed_value differs fundamentally from expected_value -> FAILURE ('content_mismatch').

RULE 3 — SUCCESS:
None of the above failure conditions matched. Target found (or navigation/overlay exception
applied), and value confirmed or N/A -> is_failure = false, failure_type = "none".
CONTENT EXPANSION SUCCESS: If the Observer reports "New content appeared" (new sections, lists,
cards, category selectors, or price panels visible in POST that were absent in PREV), the action
produced a meaningful UI response and is SUCCESS — even if the clicked button is still visible
in POST (it may remain as a contextual element within the expanded view).
{log_rules}

[BEHAVIORAL COMPARISON — Required for ALL steps, including passes.]
You MUST fill expected_behavior and observed_behavior for EVERY step. These are never empty.
- expected_behavior: Derive from ACTION TYPE + INSTRUCTION + TARGET ELEMENT. What screen state
  or UI change should result from a successful action?
  Examples:
    * click "[field name]" -> "Clicking the '[field name]' input field should open its selection interface or activate the field for input."
    * click "[item label]" -> "Clicking the '[item label]' item should select it and navigate to the next step in the flow."
    * abstractVerification -> "The screen should display the expected state or value confirming the prior action completed correctly."
    * waitUntil element appears -> "The target element should become visible on screen after the action completes."
- observed_behavior: Derive from OBSERVER REPORT. What did the Observer actually see happen?
  Examples:
    * "The selection interface opened and displayed a list of available options."
    * "The app navigated to the next screen but the displayed value '[observed value]' does not match the expected '[expected value]'."
    * "The screen shows the expected state with the correct value confirmed in POST."

[PRIOR FAILURE HISTORY — PHASE 2 only. Never use to decide is_failure.]
{prior_context}

OUTPUT VALID JSON ONLY:
{{
    "expected_value": "Specific DATA VALUE from abstractVerification instruction (a numeric value, price, date, or quoted identifier), or 'N/A' for click/tap/navigation steps.",
    "observed_value": "Exact data value confirmed in POST by Observer, or 'N/A' for tap steps, or 'NOT CONFIRMED' if verification step failed.",
    "expected_behavior": "1-2 sentences: What SHOULD happen when this action succeeds, based on ACTION TYPE and INSTRUCTION.",
    "observed_behavior": "1-2 sentences: What ACTUALLY happened according to the Observer Report.",
    "is_failure": boolean,
    "failure_type": "element_missing" | "action_failed" | "content_mismatch" | "system_error" | "none",
    "observation": "One-line verdict summary.",
    "root_cause": "If failed: direct cause + any relevant prior failure from history. If passed: empty string.",
    "confidence": A float 0.0–1.0 representing your certainty in the verdict above.
        0.90–1.00 = all evidence strongly agrees (clear visual signal, logs confirm, no ambiguity).
        0.70–0.89 = mostly agrees, minor ambiguity (e.g., partial target text match, log absent but visual clear).
        0.50–0.69 = notable ambiguity (conflicting visual/log signals, semantic equivalence applied, unclear screen state).
        0.30–0.49 = high uncertainty (missing images, system overlay, ambiguous UI state, parse fallback used).
        Use the full range — do NOT anchor to 0.9 for every step.
}}"""

    decider_messages = [
        {"role": "system", "content": "You are a JSON-only QA verdict engine. You MUST respond with a single raw JSON object. No markdown, no explanation, no text before or after the JSON."},
        {"role": "user", "content": user_prompt},
    ]
    response = _api_call_with_retry(lambda: _get_client().chat.completions.create(
        model="gpt-4o", temperature=0, max_tokens=450,
        response_format={"type": "json_object"}, messages=decider_messages,
    ))
    raw_text = response.choices[0].message.content
    parsed_dict = robust_json_extract(raw_text)

    if not parsed_dict:
        parsed_dict = {"is_failure": True, "failure_type": "system_error", "observation": "Logic agent parse failed.", "root_cause": raw_text}

    try:
        return FinalDecision(**parsed_dict)
    except Exception:
        return FinalDecision(is_failure=True, observation="Parse fallback", root_cause="JSON parsing error")


# =========================================================
# LOG OBSERVER — Parses per-step log files (GPT-4o-mini)
# =========================================================

_VALID_LEVELS  = {"info", "warning", "error", "critical"}
_VALID_SOURCES = {"network", "console", "system", "application"}


def _normalize_log_obs_dict(parsed: dict) -> None:
    """Coerce GPT-4o-mini output in-place so LogObservation(**parsed) never fails.

    GPT-4o-mini often echoes the raw uppercase level values ("ERROR", "INFO")
    from the log text back into its structured output. Pydantic Literal fields
    reject these, causing a ValidationError. Similarly, missing latency_ms
    values are returned as null/string, breaking the float field.
    """
    for err in parsed.get("relevant_errors") or []:
        if not isinstance(err, dict):
            continue
        lvl = str(err.get("level", "")).lower()
        err["level"] = lvl if lvl in _VALID_LEVELS else "info"
        src = str(err.get("source", "")).lower()
        err["source"] = src if src in _VALID_SOURCES else "application"

    for evt in parsed.get("network_events") or []:
        if not isinstance(evt, dict):
            continue
        try:
            evt["latency_ms"] = float(str(evt.get("latency_ms") or 0).replace("ms", "").strip())
        except (ValueError, TypeError):
            evt["latency_ms"] = 0.0
        try:
            evt["status_code"] = int(evt.get("status_code") or 0)
        except (ValueError, TypeError):
            evt["status_code"] = 0


def run_log_observer(inputs: dict) -> LogObservation:
    """Parse system/network logs for a single step.

    Accepts either:
      - 'log_text': pre-sliced log string (unified log.json mode), OR
      - 'log_file_path': path to a per-step log file (legacy mode)

    Returns LogObservation(has_logs=False) immediately if no log data exists.
    """
    # Unified mode: caller already sliced the relevant entries
    log_text = inputs.get('log_text', '')

    if not log_text:
        # Legacy mode: read from per-step file
        log_path = inputs.get('log_file_path', '')
        if not log_path or not os.path.exists(log_path):
            return LogObservation(has_logs=False)
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                log_text = f.read()
        except Exception:
            return LogObservation(has_logs=False)

    # Normalize level casing: log.json uses uppercase ("INFO", "ERROR") but
    # LogEntry.level is Literal["info","warning","error","critical"] (lowercase).
    # Normalize here so GPT-4o-mini receives consistent casing and Pydantic
    # validation doesn't silently drop real error entries from the output.
    log_text = (log_text
                .replace('"INFO"',     '"info"')
                .replace('"ERROR"',    '"error"')
                .replace('"WARNING"',  '"warning"')
                .replace('"CRITICAL"', '"critical"')
                .replace('"DEBUG"',    '"info"'))

    if not log_text.strip():
        return LogObservation(has_logs=False)

    # Use GPT-4o-mini (cheaper, sufficient for log parsing)
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
        {{"timestamp": "...", "level": "error", "source": "application", "message": "..."}}
    ],
    "network_events": [
        {{"method": "GET", "url": "/api/path/here", "status_code": 200, "latency_ms": 150.0, "error_message": ""}}
    ],
    "has_network_failure": false,
    "has_system_error": false,
    "latency_anomaly": false
}}

RULES:
- has_network_failure = true if ANY request returned 4xx/5xx or timed out.
- has_system_error = true if ANY log entry at "error" or "critical" level exists.
- latency_anomaly = true if ANY request took longer than 5000ms.
- If no errors found, set relevant_errors to [] and all booleans to false.
- Include at most 5 entries in relevant_errors and network_events.

FIELD MAPPING (log entries may use nested objects):
- For network_events: extract method from request.method, url from request.endpoint (or request.url), status_code from response.status_code, latency_ms from response.latency_ms.
- For source field in relevant_errors: ONLY use one of: "network", "console", "system", "application". Map "vlm_evaluator" and any other unknown source to "application".
- For level field: ONLY use one of: "info", "warning", "error", "critical"."""

    log_messages = [{"role": "user", "content": prompt}]
    response = _api_call_with_retry(lambda: _get_client().chat.completions.create(
        model="gpt-4o-mini", temperature=0, max_tokens=600,
        response_format={"type": "json_object"}, messages=log_messages,
    ))
    raw_text = response.choices[0].message.content
    parsed = robust_json_extract(raw_text)

    if not parsed:
        return LogObservation(has_logs=True, log_summary="Log parsing failed — could not extract structured data.")

    parsed['has_logs'] = True
    _normalize_log_obs_dict(parsed)
    try:
        return LogObservation(**parsed)
    except Exception as e:
        return LogObservation(has_logs=True, log_summary=f"Log parsing validation failed: {e}")


# =========================================================
# LANGCHAIN RUNNABLE WRAPPERS
# =========================================================
vision_chain = RunnableLambda(run_vision_observer)
logic_chain = RunnableLambda(run_logic_decider)
log_chain = RunnableLambda(run_log_observer)
