# Describer-Decider Engine v32.3 — Full Project Explanation

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [The Core Hypothesis](#2-the-core-hypothesis)
3. [Environment & Hardware](#3-environment--hardware)
4. [Tech Stack — Every Tool & Library Explained](#4-tech-stack--every-tool--library-explained)
5. [Architecture Overview](#5-architecture-overview)
6. [Data Format — Input Files](#6-data-format--input-files)
7. [File 1: models.py — Data Structures & Helpers](#7-file-1-modelspy--data-structures--helpers)
8. [File 2: agents.py — The AI Brains](#8-file-2-agentspy--the-ai-brains)
9. [File 3: graph.py — The Pipeline Orchestration](#9-file-3-graphpy--the-pipeline-orchestration)
10. [File 4: runner.py — The Entry Point](#10-file-4-runnerpy--the-entry-point)
11. [End-to-End Walkthrough — What Happens When You Press Run](#11-end-to-end-walkthrough--what-happens-when-you-press-run)

---

## 1. Project Overview

This is an **AI-powered fault localization** system for UI-driven applications (mobile/web). Given a test execution trace (a sequence of screenshots + action descriptions), the system automatically determines whether each step **passed or failed** — and if it failed, **why**.

Think of it as an automated QA tester that looks at screenshots the same way a human tester would, but uses AI vision models to do it.

**Practical Example:** A test script says "Click the 'Nereden' (From Where) field." The system receives a screenshot before the click and a screenshot after the click. It uses a vision AI to observe what changed, then uses a reasoning AI to decide: did the click work? Did the right screen appear?

---

## 2. The Core Hypothesis

> A Vision-Language Model (VLM) can observe UI state changes between a "before" and "after" screenshot, and a separate reasoning LLM can then determine whether the application behaved correctly — producing structured, machine-readable fault reports.

The key design insight is **separation of concerns**: one AI agent **observes** (describes what it sees), and a different AI agent **decides** (judges pass/fail). This prevents the observer from being biased by the decision task, and lets the decider work from clean textual evidence.

---

## 3. Environment & Hardware

| Item | Detail |
|---|---|
| **Runtime** | Google Colab (cloud Jupyter notebook) |
| **GPU** | NVIDIA A100 with 40 GB VRAM (via Colab Pro) |
| **Storage** | Google Drive (mounted at `/content/drive`) |
| **Python** | 3.10+ |

**Why Colab?** We need a powerful GPU to run two large AI models simultaneously. The A100's 40 GB of VRAM can hold both the 7B-parameter vision model (~14 GB) and the 14B-parameter logic model (~28 GB) in bfloat16 precision at the same time. No need to swap models in and out of memory.

**Why Google Drive?** Colab VMs are ephemeral — they reset on disconnect. Drive persists model weights (~42 GB) across sessions so we don't re-download them every time.

---

## 4. Tech Stack — Every Tool & Library Explained

### Core AI Libraries

| Library | Version | What It Does | Why We Use It |
|---|---|---|---|
| **transformers** | `==4.49.0` (pinned) | Hugging Face's library for loading and running AI models. Provides the `from_pretrained()` API to download model weights and the `generate()` method to produce text output. | This is the standard way to load and run open-source LLMs. Pinned to 4.49.0 because versions >=4.50 break Qwen2.5-VL with Triton compilation errors and 15% accuracy loss. |
| **accelerate** | latest | Handles distributing model layers across GPU/CPU automatically via `device_map="auto"`. | Without it, we'd have to manually assign each model layer to a device. Accelerate does this automatically and optimally. |
| **qwen-vl-utils** | `==0.0.8` | Qwen's official utility for processing vision inputs — converts image paths into the token format Qwen2.5-VL expects. | The VL model needs images preprocessed in a specific way (resized, normalized, converted to visual tokens). This utility handles that. |
| **huggingface_hub** | latest | Authenticates with Hugging Face servers to download gated model weights. | Some models require accepting a license. This handles the API token authentication. |
| **torchvision** | latest | Image transformation utilities used internally by the vision pipeline. | Dependency of qwen-vl-utils for image preprocessing. |

### Framework Libraries

| Library | What It Does | Why We Use It |
|---|---|---|
| **LangChain** (`langchain`, `langchain-core`) | A framework for building LLM-powered applications. We use `RunnableLambda` to wrap our Python functions into standardized callable units. | Gives us a uniform `.invoke()` interface for all AI agents. Makes it easy to chain agents together, swap implementations, and add logging. |
| **LangGraph** (`langgraph`) | LangChain's orchestration layer for building stateful, multi-step AI pipelines as directed graphs. | Our pipeline is a loop: for each step, route → observe → decide → accumulate → next step. LangGraph models this as a graph with nodes (processing steps) and edges (transitions). It manages state automatically. |
| **Pydantic** (`pydantic` v2) | Data validation library. Every piece of data flowing between agents is a Pydantic model with strict type checking. | Prevents bugs from misspelled keys or wrong data types. If the AI returns malformed JSON, Pydantic catches it immediately instead of causing cryptic errors downstream. |

### Utility Libraries

| Library | What It Does | Why We Use It |
|---|---|---|
| **nest_asyncio** | Patches Python's event loop to allow nested `async` calls. | Colab already runs an async event loop. LangGraph internally uses async. Without this patch, you get "event loop already running" errors. |
| **torch** (PyTorch) | The deep learning framework that actually runs the neural networks on the GPU. | Both Qwen models are PyTorch models. All tensor operations, GPU memory management, and inference happen through PyTorch. |
| **PIL** (Pillow) | Image loading and manipulation. | Used to open PNG screenshot files before passing them to the vision model. |

### AI Models (Not Libraries — These Are the Actual Neural Networks)

| Model | Parameters | Role | What It Does |
|---|---|---|---|
| **Qwen2.5-VL-7B-Instruct** | 7 billion | **Vision Observer** ("Describer") | A Vision-Language Model that can "see" images and describe them in text. We feed it two screenshots (before/after) and it describes what it observes: which UI elements are present, what changed, what text it can read (OCR). |
| **Qwen2.5-14B-Instruct** | 14 billion | **Logic Decider** ("Decider") | A text-only reasoning model. It receives the Observer's text description and the step's expected action, then applies structured rules to decide: PASS or FAIL. It never sees the images directly. |

**Why two separate models instead of one?**
- The vision model is good at *seeing* but can hallucinate judgments.
- The logic model is good at *reasoning* but can't see images.
- By separating observation from judgment, we get more reliable, auditable results.

### Technical Configuration Choices

| Setting | Value | Reason |
|---|---|---|
| **torch_dtype=torch.bfloat16** | 16-bit brain floating point | Halves memory usage (vs. float32) with minimal accuracy loss. A100 has native bfloat16 hardware support. We use `torch_dtype` specifically because `dtype` is silently ignored by `from_pretrained()`. |
| **attn_implementation="eager"** | Standard attention (no Flash/SDPA) | Flash Attention and SDPA require Triton JIT compilation, which crashes on Colab due to a corrupted CUDA toolchain. Eager mode is slower but stable. |
| **do_sample=False** | Greedy decoding (deterministic) | Academic research requires reproducible results. With do_sample=False, the model always picks the highest-probability token — same input always produces same output. |
| **max_pixels=768×28×28** | ~602K pixels per image | Controls how many visual tokens each image produces. Lower than default (1M) to keep GPU memory usage manageable with 2 images + eager attention. |
| **JIT disabled** (TorchDynamo, NVFuser, legacy fuser) | All 3 systems off | Prevents PyTorch from trying to compile optimized GPU kernels at runtime, which crashes on Colab due to missing NVRTC builtins. |

---

## 5. Architecture Overview

### The Two-Agent Pipeline

```
"Describer"                    "Decider"
(Qwen2.5-VL-7B)              (Qwen2.5-14B)
    |                              |
    |  sees 2 screenshots          |  reads text description
    |  describes what changed      |  applies rules
    |  outputs text observations   |  outputs PASS/FAIL verdict
    v                              v
[VisionObservation]  ------>  [FinalDecision]
```

### The LangGraph Pipeline (What Runs For Each Step)

```
START
  |
  v
[router_node] ---> Is this step done? ---> YES ---> END (print report)
  |
  |---> Should we skip this step? ---> YES ---> [skip_node] ---> back to router
  |
  |---> Analyze this step:
  |       |
  |       v
  |     [vision_node]  --- Qwen2.5-VL looks at screenshots
  |       |
  |       v
  |     [log_node]     --- Check for system logs (if any exist)
  |       |
  |       v
  |     [decider_node] --- Qwen2.5-14B makes PASS/FAIL decision
  |       |
  |       v
  |     [accumulate_node] --- Record result, advance counter
  |       |
  |       v
  |     back to router (next step)
```

---

## 6. Data Format — Input Files

### Folder Structure
```
steps/
  steps.json          <-- Action log (what the test did)
  step_0_prev.png     <-- Screenshot BEFORE step 0
  step_0_post.png     <-- Screenshot AFTER step 0
  step_7_prev.png     <-- Screenshot BEFORE step 7
  step_7_post.png     <-- Screenshot AFTER step 7
  ...                 <-- One pair per step
  step_N_logs.json    <-- (optional) System logs for step N
```

### steps.json Structure

Each entry in the JSON array represents one test step:
```json
{
  "action": "click",              // What type of action: click, waitUntil, wait, abstractVerification
  "element": "Nereden",           // The UI element to interact with
  "intent": "Click on Nereden",   // Why this action is being performed
  "stepInstruction": "Click on the 'Nereden' field.",  // Human-readable instruction
  "step_idx": 7,                  // Step number (maps to screenshot filenames)
  "requiresVLM": true,            // Should the vision model analyze this?
  "useVLMCoordinates": true       // Was VLM used to find the click coordinates?
}
```

For `waitUntil` steps, the real target element is nested inside a `condition` object:
```json
{
  "action": "waitUntil",
  "condition": {
    "element": "Tüm seyahatin tek uygulamada",
    "stepInstruction": "Check if element exists on screen."
  },
  "step_idx": 0
}
```

---

## 7. File 1: models.py — Data Structures & Helpers

**Purpose:** Defines all data structures (Pydantic models) and utility functions used across the system. This is the "vocabulary" of the project — every piece of data that flows between components is defined here.

### Lines 1-10: Imports

```python
import os              # File path operations (checking if files exist)
import json            # Parsing JSON files (steps.json, log files)
import re              # Regular expressions (extracting JSON from AI model output)
from typing import TypedDict, List, Dict, Any, Optional, Literal  # Type annotations

from pydantic import BaseModel, Field   # Data validation framework
```

These are standard Python imports. `TypedDict` is used for the graph state (LangGraph requires it). `Pydantic` is used for all AI agent inputs/outputs.

---

### Lines 17-20: VisionObservation — What the Vision AI Sees

```python
class VisionObservation(BaseModel):
    prev_screen_elements: str = Field(default="Could not analyze PREV screen.")
    post_screen_changes: str = Field(default="Could not analyze POST screen.")
    semantic_screen_summary: str = Field(default="Could not determine screen state.")
```

This is the **output of the Vision Observer**. Three text fields:

| Field | What It Contains | Example |
|---|---|---|
| `prev_screen_elements` | What the AI saw in the BEFORE screenshot — target element presence, key UI elements | `"Target 'Nereden' is visible as a large green text field on the flight search form."` |
| `post_screen_changes` | What changed in the AFTER screenshot — target presence, visual differences, OCR readings | `"The screen now shows the airport picker. 'Nereden:' appears in the search bar."` |
| `semantic_screen_summary` | A 1-2 sentence high-level description of the screen state after the action | `"The app navigated from the search form to the departure airport selection screen."` |

The `Field(default=...)` values are fallbacks — if the AI model crashes or returns garbage, these defaults prevent the entire pipeline from crashing.

---

### Lines 22-28: FinalDecision — The Pass/Fail Verdict

```python
class FinalDecision(BaseModel):
    expected_value: str = Field(default="N/A")
    observed_value: str = Field(default="N/A")
    is_failure: bool
    failure_type: str = Field(default="none")
    observation: str
    root_cause: str = Field(default="")
```

This is the **output of the Logic Decider**. The final verdict for each step:

| Field | What It Contains | Example (PASS) | Example (FAIL) |
|---|---|---|---|
| `expected_value` | What value we expected to see (from the instruction), or "N/A" if no specific value | `"N/A"` | `"12:15"` |
| `observed_value` | What value was actually seen in the screenshot, or "N/A" | `"N/A"` | `"21:15"` |
| `is_failure` | **The core verdict**: `true` = step failed, `false` = step passed | `false` | `true` |
| `failure_type` | Category of failure: `element_missing`, `action_failed`, `content_mismatch`, `system_error`, or `none` | `"none"` | `"content_mismatch"` |
| `observation` | Brief summary of what the decider observed | `"Target found, action completed"` | `"Expected 12:15 but observed 21:15"` |
| `root_cause` | If failed: explanation of why. Can reference prior step failures. | `""` | `"Wrong flight selected. (Likely caused by Step 17)"` |

`is_failure` has no default — it's **required**. The AI model MUST produce a true/false value. If it doesn't, parsing fails and the system falls back to a failure verdict (conservative approach).

---

### Lines 34-57: Log Models — For System Log Analysis (Phase 2)

```python
class LogEntry(BaseModel):
    timestamp: str = Field(default="")
    level: Literal["info", "warning", "error", "critical"] = Field(default="info")
    source: Literal["network", "console", "system", "application"] = Field(default="application")
    message: str = Field(default="")
```

A single parsed log event (e.g., a console error, a network timeout). `Literal` restricts the value to one of the listed options — Pydantic will reject any other value.

```python
class NetworkEvent(BaseModel):
    method: str = Field(default="")           # "GET", "POST", etc.
    url: str = Field(default="")              # The API endpoint called
    status_code: int = Field(default=0)       # HTTP status (200=ok, 500=server error)
    latency_ms: float = Field(default=0.0)    # How long the request took
    error_message: str = Field(default="")    # Error text if request failed
```

A single HTTP request/response captured during the step.

```python
class LogObservation(BaseModel):
    has_logs: bool = Field(default=False)              # Were any logs found for this step?
    log_summary: str = Field(default="No logs available for this step.")
    relevant_errors: List[LogEntry] = Field(default_factory=list)    # Parsed error entries
    network_events: List[NetworkEvent] = Field(default_factory=list) # Parsed HTTP events
    has_network_failure: bool = Field(default=False)   # Any 4xx/5xx HTTP errors?
    has_system_error: bool = Field(default=False)      # Any error/critical log entries?
    latency_anomaly: bool = Field(default=False)       # Any request > 5000ms?
```

The **output of the Log Observer**. This agent parses raw system logs into a structured summary. `has_logs=False` means no log file existed for this step — the Log Observer did nothing.

`default_factory=list` means "create a new empty list for each instance" — a Pydantic requirement for mutable defaults.

---

### Lines 63-79: GraphState — LangGraph's Shared Memory

```python
class GraphState(TypedDict):
    # Persistent state (accumulates across ALL steps)
    current_step_index: int                  # Which step we're currently processing
    steps_data: List[dict]                   # The full steps.json array
    trace_files_path: str                    # Path to the folder with screenshots
    final_report: str                        # The final text report (built at the end)
    stop_execution: bool                     # Flag: should the graph stop?
    detected_errors: List[dict]              # All failures found so far
    active_failures: List[str]               # Human-readable failure history
    active_failure_map: Dict[int, dict]      # step_index → FinalDecision dict

    # Transient per-step state (overwritten each iteration by nodes)
    _current_ctx: Optional[dict]             # Current step's extracted context
    _current_vision_obs: Optional[dict]      # Current step's vision observation
    _current_log_obs: Optional[dict]         # Current step's log observation
    _current_decision: Optional[dict]        # Current step's final decision
    _route: Optional[str]                    # "skip" or "analyze"
    _skip_reason: Optional[str]              # Why this step was skipped
```

This is the **central state object** that LangGraph passes between all nodes. Every node reads from it and writes back to it.

**Persistent fields** accumulate data across the entire run (error lists grow, step counter increments).
**Transient fields** (prefixed with `_`) are scratch space — each step overwrites them. They let nodes pass data to downstream nodes within the same step iteration.

`TypedDict` (not Pydantic) is used because LangGraph requires it — it treats state as a plain dictionary internally.

---

### Lines 85-93: load_project_data() — Loading the Test Script

```python
def load_project_data(folder_path: str) -> List[dict]:
    for filename in ["steps.json", "fail.json"]:        # Try both filenames
        path = os.path.join(folder_path, filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list): return data                      # Direct array
                if isinstance(data, dict) and "stepDetails" in data:
                    return data["stepDetails"]                              # Nested format
    return []  # Nothing found
```

Loads the step definitions from a JSON file. Supports two file formats:
1. A plain JSON array `[{step1}, {step2}, ...]`
2. A nested object `{"stepDetails": [{step1}, {step2}, ...]}`

It tries `steps.json` first, then `fail.json` as a fallback.

---

### Lines 95-96: format_active_failures() — Formatting Failure History

```python
def format_active_failures(failures: List[str]) -> str:
    return "\n".join(failures) if failures else "None. System is healthy so far."
```

Simple utility: joins the list of failure descriptions into a newline-separated string for display. If the list is empty, returns a reassuring message.

---

### Lines 98-117: robust_json_extract() — Parsing AI Output

```python
def robust_json_extract(text: str) -> dict:
    # 1. Try markdown code block first (```json {...} ``` or ``` {...} ```)
    code_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if code_match:
        clean = code_match.group(1).replace("True","true").replace("False","false").replace("None","null")
        try:
            return json.loads(clean)
        except Exception:
            pass

    # 2. Fallback: greedy match from first { to last }
    match = re.search(r'\{.*\}', text.strip(), re.DOTALL)
    if match:
        clean = match.group(0).replace("True","true").replace("False","false").replace("None","null")
        try:
            return json.loads(clean)
        except Exception:
            pass

    return {}  # Complete parse failure
```

**This is critical.** AI models don't always produce clean JSON. They might add markdown formatting, explanatory text, or Python-style booleans. This function handles all of that:

1. **First attempt**: Look for JSON inside a markdown code block (` ```json {...} ``` `). AI models often wrap their output this way.
2. **Second attempt**: Extract everything between the first `{` and the last `}` in the raw text — the "greedy" approach.
3. **Cleanup**: Replace Python-style `True`/`False`/`None` with JSON-standard `true`/`false`/`null`.
4. **Fallback**: If both attempts fail, return an empty dict. Callers handle this by producing a default failure verdict.

---

### Lines 119-143: extract_step_context() — Normalizing Step Data

```python
def extract_step_context(raw_item: dict, current_idx: int) -> dict:
    step_data = raw_item.get('step', raw_item) if isinstance(raw_item, dict) else raw_item
    action = step_data.get('action', 'unknown')
```

The first line handles two possible input formats: `{"step": {...actual data...}}` (nested) or `{...actual data...}` (flat). It always extracts the inner data.

```python
    # Bug 2 fix: waitUntil steps nest the real element/instruction inside 'condition'.
    condition = step_data.get('condition', {}) if action == 'waitUntil' else {}
```

**Bug 2 fix:** For `waitUntil` actions, the target element isn't at the top level — it's buried inside a `condition` sub-object. Without this, the system would extract `element: null` and skip the step incorrectly.

```python
    instruction = (step_data.get('stepInstruction') or step_data.get('instruction')
                   or condition.get('stepInstruction') or condition.get('intent', 'None'))
```

Extracts the human-readable instruction, trying multiple possible field names in priority order (the `or` chain short-circuits at the first non-empty value).

```python
    target = step_data.get('element')
    if not target or str(target).strip() == "" or str(target).strip().lower() == "null":
        target = condition.get('element') or condition.get('intent') if condition else None
    if not target or str(target).strip() == "":
        target = step_data.get('intent', 'General verification based on instruction')
```

Extracts the target element, with three levels of fallback:
1. Direct `element` field
2. `element` from the `condition` sub-object (for `waitUntil` steps)
3. `intent` field as last resort

```python
    requires_vlm = step_data.get('requiresVLM', True)  # Default: yes, use vision

    return {
        "step_num": step_data.get('step_idx', current_idx),
        "action": action,
        "instruction": instruction,
        "target": target,
        "requires_vlm": requires_vlm,
    }
```

Returns a clean, normalized dictionary that all downstream nodes can rely on having the same structure regardless of the input format.

---

## 8. File 2: agents.py — The AI Brains

**Purpose:** Loads the AI models into GPU memory and defines the three AI agents (Vision Observer, Logic Decider, Log Observer) as LangChain runnables.

### Lines 1-28: PyTorch JIT Safety Guards

```python
import torch._dynamo
torch._dynamo.config.disable = True                    # Disable TorchDynamo
torch._C._jit_set_profiling_executor(False)            # Disable profiling executor
torch._C._jit_set_profiling_mode(False)                # Disable profiling mode
try:
    torch._C._jit_set_nvfuser_enabled(False)           # Disable NVFuser
except AttributeError:
    pass                                                # OK if removed in newer PyTorch
torch._C._jit_override_can_fuse_on_gpu(False)          # Disable GPU kernel fusion
torch._C._jit_override_can_fuse_on_cpu(False)          # Disable CPU kernel fusion
```

**Why?** PyTorch has three JIT (Just-In-Time) compilation systems that try to optimize GPU operations by compiling custom CUDA kernels at runtime:

1. **TorchDynamo** — The newest compiler (`torch.compile`). Crashes on Colab because the Triton backend fails.
2. **NVFuser** — NVIDIA's kernel fusion engine. Crashes because it needs NVRTC (runtime compiler) which is broken on Colab.
3. **Legacy Fuser** — The oldest system. Generates `OffsetCalculator.cuh` kernels that also need NVRTC.

By disabling all three, we force PyTorch to use pre-compiled kernels only. Slower, but guaranteed to work on Colab.

---

### Lines 30-37: Imports

```python
from google.colab import drive                # Mount Google Drive
from huggingface_hub import login             # Authenticate with Hugging Face

from transformers import AutoProcessor, AutoModelForCausalLM, AutoTokenizer
from transformers import Qwen2_5_VLForConditionalGeneration  # Vision model class
from qwen_vl_utils import process_vision_info                # Image preprocessing
from langchain_core.runnables import RunnableLambda           # LangChain wrapper

from models import VisionObservation, FinalDecision, LogObservation, robust_json_extract
```

Key imports:
- `Qwen2_5_VLForConditionalGeneration` — The specific model architecture class for Qwen2.5-VL (vision-language model). Different from `AutoModelForCausalLM` because it has special image-processing layers.
- `AutoModelForCausalLM` — Generic "auto" class that loads any causal language model. Used for the logic model (text-only).
- `RunnableLambda` — Wraps a plain Python function into a LangChain "Runnable" that can be `.invoke()`d.

---

### Lines 44-47: Module-Level Model References

```python
_vision_model = None
_vision_processor = None
_logic_model = None
_logic_tokenizer = None
```

Global variables that hold the loaded models. Set to `None` initially; filled by `load_models()`. The underscore prefix is a Python convention meaning "private — don't access these directly from other files."

---

### Lines 53-117: load_models() — Loading AI Models into GPU

```python
def load_models():
    global _vision_model, _vision_processor, _logic_model, _logic_tokenizer
```

`global` tells Python these aren't local variables — we're modifying the module-level ones.

```python
    if not os.path.exists('/content/drive'):
        drive.mount('/content/drive')
```

Mounts Google Drive if not already mounted. This makes your Drive files accessible at `/content/drive/MyDrive/`.

```python
    hf_cache = "/content/drive/MyDrive/hf_cache"
    os.makedirs(hf_cache, exist_ok=True)
    os.environ["HF_HOME"] = hf_cache
```

**Redirects the Hugging Face download cache to Google Drive.** Without this, the ~42 GB of model weights would download to Colab's local disk (which gets wiped on disconnect). With this, weights persist on Drive and only download once ever.

```python
    if "HF_TOKEN" not in os.environ:
        os.environ["HF_TOKEN"] = input("Hugging Face Token: ")
    login(token=os.environ["HF_TOKEN"])
```

Authenticates with Hugging Face. Some models are "gated" — you must accept a license agreement and use an API token.

```python
    _vision_model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        "Qwen/Qwen2.5-VL-7B-Instruct",
        device_map="auto",                    # Automatically place layers on GPU/CPU
        torch_dtype=torch.bfloat16,           # 16-bit precision (halves memory)
        attn_implementation="eager",          # No Flash Attention (Colab compat)
    )
```

**Loads the Vision Model.** `from_pretrained()` downloads the model weights from Hugging Face (or loads from Drive cache), creates the neural network architecture, and loads the weights into it. `device_map="auto"` uses the `accelerate` library to optimally distribute layers across available GPU(s) and CPU.

```python
    _vision_processor = AutoProcessor.from_pretrained(
        "Qwen/Qwen2.5-VL-7B-Instruct",
        min_pixels=256 * 28 * 28,   # 200,704  — minimum image resolution
        max_pixels=768 * 28 * 28,   # 602,112  — maximum image resolution (capped)
    )
```

**Loads the Processor** (tokenizer + image preprocessor). The processor converts raw images + text into the numerical format the model expects. `max_pixels` is capped to control memory: default ~1M pixels would produce ~5120 visual tokens per image. With 2 images and eager attention, the O(n^2) attention matrix would exceed 20 GB. Our cap keeps it at ~3072 tokens/image (~2.5 GB).

```python
    _vision_model.generation_config.do_sample = False
    _vision_model.generation_config.temperature = 1.0
    _vision_model.generation_config.top_p = 1.0
    _vision_model.generation_config.top_k = 50
```

Sets deterministic decoding. `do_sample=False` means greedy decoding (always pick the most probable token). The temperature/top_p/top_k values are set to neutral defaults to suppress warnings that arise when do_sample=False conflicts with the model's bundled configuration.

```python
    _logic_model = AutoModelForCausalLM.from_pretrained(
        logic_model_name,
        device_map="auto",
        torch_dtype=torch.bfloat16,
        attn_implementation="eager",
    )
    _logic_tokenizer = AutoTokenizer.from_pretrained(logic_model_name)
```

**Loads the Logic Model** (text-only, 14B parameters). Same approach as the vision model. Uses `AutoModelForCausalLM` because it doesn't need image processing capabilities. The tokenizer converts text to/from token IDs.

---

### Lines 122-200: run_vision_observer() — The "Eye" Agent

```python
def run_vision_observer(inputs: dict) -> VisionObservation:
```

This function IS the Vision Observer. It takes step context + two image paths, calls the vision AI model, and returns a `VisionObservation`.

**The Prompt (lines 124-172):**

The prompt is a detailed instruction to the vision model, structured as:

1. **Role Assignment:** `"You are an objective UI Observer. You DO NOT make pass/fail decisions."` — Forces the model to only describe, never judge.

2. **[CONTEXT] Block:** Injects the current step's action type, instruction, and target element. This tells the model what to look for.

3. **TASK 1 — TARGET SEARCH:** The core observation task. Key sub-rules:
   - **SEMANTIC EQUIVALENCE:** The target name may not exactly match the UI text. E.g., "Çarpı" (Turkish for "cross") = the X close button. The model must match by meaning.
   - **SELECTED STATE:** If an element is highlighted/active in POST, it's "present" even if it looks different from the plain state.
   - Must explicitly state: target in PREV only / POST only / Both / Neither.

4. **TASK 2 — VISUAL CHANGES:** Describe what changed between the two screenshots.
   - **OVERLAY DETECTION:** If a system dialog (like "Allow notifications?") appeared, describe BOTH the dialog AND the screen behind it.
   - **STRICT VALUE OCR:** If the instruction mentions a specific value (like a time "12:15"), the model must read exact characters from the POST image. No guessing.

5. **TASK 3 — SEMANTIC SCREEN SUMMARY:** A 1-2 sentence high-level summary. This gives the Logic Decider global context (e.g., "The app navigated to the airport picker").

6. **Output Format:** Strict JSON with exactly 3 fields. The `{{` and `}}` are escaped braces in Python f-strings (they produce literal `{` and `}` in the output).

**The Inference Call (lines 174-194):**

```python
    messages = [
        {"role": "user", "content": [
            {"type": "image", "image": inputs["prev"]},    # PREV screenshot
            {"type": "image", "image": inputs["post"]},    # POST screenshot
            {"type": "text", "text": raw_text_prompt}       # The instruction prompt
        ]}
    ]
```

Constructs the input message in the chat format Qwen2.5-VL expects. Two images + one text prompt in a single user message.

```python
    text = _vision_processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs = process_vision_info(messages)
```

`apply_chat_template()` converts the message into the model's expected text format (adds special tokens like `<|im_start|>` and `<|im_end|>`). `process_vision_info()` preprocesses the images (resize, normalize pixels).

```python
    model_inputs = _vision_processor(
        text=[text], images=image_inputs, videos=video_inputs,
        padding=True, return_tensors="pt"
    ).to(_vision_model.device)
```

Combines text tokens and image tokens into a single input tensor, moves it to the GPU.

```python
    torch.cuda.empty_cache()  # Free stale GPU memory before heavy inference

    with torch.no_grad():     # Disable gradient tracking (we're not training)
        generated_ids = _vision_model.generate(**model_inputs, max_new_tokens=350, do_sample=False)
```

`generate()` runs the actual AI inference — the model "thinks" and produces output tokens. `max_new_tokens=350` limits the output length. `torch.no_grad()` saves significant GPU memory by not tracking gradients (only needed during training).

```python
    generated_ids_trimmed = [out_ids[len(in_ids):] for in_ids, out_ids in
                             zip(model_inputs.input_ids, generated_ids)]
    generated_text = _vision_processor.batch_decode(
        generated_ids_trimmed, skip_special_tokens=True,
        clean_up_tokenization_spaces=False
    )[0]
```

The model outputs ALL tokens (input + generated). We trim off the input tokens to keep only the new generated text, then decode token IDs back into a string.

```python
    parsed_dict = robust_json_extract(generated_text)
    try:
        return VisionObservation(**parsed_dict)
    except:
        return VisionObservation(prev_screen_elements="Failed to parse", ...)
```

Extracts JSON from the model's raw text output, creates a validated `VisionObservation`. If parsing fails, returns a default.

---

### Lines 206-347: run_logic_decider() — The "Brain" Agent

```python
def run_logic_decider(inputs: dict) -> FinalDecision:
```

This function IS the Logic Decider. It receives the Vision Observer's text report (NOT images) and applies structured rules to decide PASS/FAIL.

**Building Prior Failure Context (lines 220-229):**

```python
    recent_failures = active_failures[-3:] if active_failures else []
```

Takes the last 3 failures from history. This is injected AFTER the decision rules in the prompt (Phase 2 only) to prevent **anchoring bias** — if the model sees failures before making its decision, it becomes biased toward finding more failures.

**Building Log Evidence (lines 231-261):**

```python
    if log_report.has_logs:
        log_section = f"""
[LOG EVIDENCE]
- LOG SUMMARY: {log_report.log_summary}
- NETWORK FAILURE DETECTED: {log_report.has_network_failure}
..."""
```

Only injected when actual system logs exist. When no logs are available, `log_section` and `log_rules` are empty strings — the prompt is byte-for-byte identical to the version without log support (zero regression).

**The Decision Prompt (lines 263-327):**

The prompt has this structure:

1. **Two-Phase Design:**
   - Phase 1: Decide PASS/FAIL based on visual evidence only.
   - Phase 2: If failed, enrich the root_cause with prior failure history.
   This separation prevents the model from being influenced by unrelated prior failures.

2. **[CONTEXT]:** The current step's action, instruction, and target element.

3. **[OBSERVER REPORT]:** The VisionObservation's three fields — this is ALL the visual evidence the Decider gets. It never sees the actual images.

4. **[DECISION RULES]:** Four rules applied in strict priority order:

   - **RULE 0 — SYSTEM OVERLAY EXCEPTION** (highest priority): If a system dialog (like "Allow notifications?") appeared, it's NOT a failure. The app's action still succeeded behind the overlay.

   - **RULE 1 — TARGET ABSENT**: If the target is missing from BOTH screenshots → failure.
     - *CLOSE/DISMISS EXCEPTION:* A close button gone in POST means the modal was successfully dismissed.
     - *NAVIGATION EXCEPTION:* Target gone in POST because the app moved to the next screen → success (e.g., clicking a button navigated away from the page where the button was).
     - *VALUE VERIFICATION OVERRIDE:* Even if navigation happened, if there's a specific value to verify, we can't just assume success — must check the value first.

   - **RULE 2 — STRICT VALUE VERIFICATION**: If the instruction mentions a specific value (like "select the 12:15 flight"), the Observer MUST have confirmed that exact value is visible in POST. Just seeing a screen change is NOT enough. If the value isn't confirmed → content_mismatch failure.

   - **RULE 3 — SUCCESS**: If none of the failure rules matched → pass.

5. **[LOG EVIDENCE RULES]** (conditional): How to incorporate log data. Visual evidence is always primary. Logs can upgrade PASS→FAIL (server error not visible in UI) but can NEVER downgrade FAIL→PASS.

6. **[PRIOR FAILURE HISTORY]:** Injected LAST. Only used for root_cause attribution, never for the verdict itself.

**The Inference Call (lines 329-347):**

Same pattern as the Vision Observer but using the text-only logic model. Key difference: the system message explicitly says `"You are a JSON-only QA verdict engine"` to prevent the model from outputting explanatory text outside the JSON.

---

### Lines 353-437: run_log_observer() — The Log Analysis Agent

```python
def run_log_observer(inputs: dict) -> LogObservation:
```

Checks for a per-step log file (`step_N_logs.json`). If no file exists, returns `LogObservation(has_logs=False)` immediately — **no LLM call** is made. This is important for performance: most steps won't have logs.

If a log file exists, the raw log text is fed to the logic model (Qwen2.5-14B) with instructions to extract structured information: error entries, network events, failure flags.

---

### Lines 443-445: LangChain Runnable Wrappers

```python
vision_chain = RunnableLambda(run_vision_observer)
logic_chain = RunnableLambda(run_logic_decider)
log_chain = RunnableLambda(run_log_observer)
```

Wraps each Python function into a LangChain `Runnable`. This provides:
- A uniform `.invoke(input_dict)` interface
- Compatibility with LangChain's chaining, logging, and error handling
- The ability to swap implementations (e.g., replace local model with API call) without changing the rest of the code

---

## 9. File 3: graph.py — The Pipeline Orchestration

**Purpose:** Defines the LangGraph graph — six nodes connected by edges that process steps in a loop. This is the "flow control" of the system.

### Lines 1-15: Imports

```python
from langgraph.graph import StateGraph, END       # Graph builder and terminal node
from models import GraphState, VisionObservation, FinalDecision, LogObservation, extract_step_context
from agents import vision_chain, logic_chain, log_chain    # The AI agent runnables
```

`StateGraph` is LangGraph's core class — you add nodes and edges to it, then `compile()` it into an executable graph. `END` is a special node that terminates execution.

---

### Lines 21-55: Node 1 — router_node (The Traffic Director)

```python
def router_node(state: GraphState):
    idx = state.get('current_step_index', 0)
    steps = state['steps_data']
```

Every node receives the full `GraphState` and returns a partial update (only the fields it changed).

```python
    # End condition: all steps processed
    if idx >= len(steps):
        # Build the final summary report
        report = f"\nTEST COMPLETE. Scanned {len(steps)} steps.\n"
        if errors:
            for err in errors: report += f"Step {err['step_index']}: {err['reason']}\n"
        else: report += "ALL PASSED.\n"
        return {"stop_execution": True, "final_report": report}
```

When all steps are processed, the router builds a summary report listing all detected failures and sets `stop_execution=True` to signal the graph to terminate.

```python
    ctx = extract_step_context(steps[idx], idx)

    # Determine skip conditions
    is_skip = (ctx["action"] == "wait"                        # Pure delay — nothing to observe
               or not ctx.get("requires_vlm", True)           # Explicitly flagged as non-visual
               or str(ctx["target"]).strip().lower() == "null") # No target element
```

Three types of steps get skipped:
1. `wait` actions — just a delay, no UI change
2. Steps with `requiresVLM: false` — the test runner already verified this step without vision
3. Steps where the target is literally "null"

```python
    images_missing = False
    if not is_skip:
        prev_path = os.path.join(folder, f"step_{step_num}_prev.png")
        post_path = os.path.join(folder, f"step_{step_num}_post.png")
        images_missing = not os.path.exists(prev_path) or not os.path.exists(post_path)
```

Even if a step should be analyzed, skip it if the screenshot files don't exist.

```python
    route = "skip" if (is_skip or images_missing) else "analyze"

    return {
        "_current_ctx": ctx,
        "_route": route,
        "_skip_reason": "images_missing" if images_missing else ("vlm_skip" if is_skip else ""),
    }
```

Writes the routing decision and extracted context to the transient state fields. The conditional edge function `route_step()` reads `_route` to decide which node runs next.

---

### Lines 58-62: route_step() — The Conditional Edge

```python
def route_step(state: GraphState) -> str:
    if state.get("stop_execution"):
        return "end"                    # → END (terminate graph)
    return state.get("_route", "analyze")  # → "skip" or "analyze"
```

LangGraph calls this function after `router_node` to decide which path to take. Returns a string that maps to a node name.

---

### Lines 68-79: Node 2 — skip_node

```python
def skip_node(state: GraphState):
    idx = state.get('current_step_index', 0)
    # Just print a message and advance the counter
    return {"current_step_index": idx + 1}
```

Does nothing except increment the step counter. Prints a message so the user knows the step was intentionally skipped.

---

### Lines 85-111: Node 3 — vision_node (Calls the Vision AI)

```python
def vision_node(state: GraphState):
    ctx = state['_current_ctx']
    folder = state['trace_files_path']
    step_num = ctx['step_num']

    prev_path = os.path.join(folder, f"step_{step_num}_prev.png")
    post_path = os.path.join(folder, f"step_{step_num}_post.png")
```

Reads the current step context (set by the router) and builds the image file paths.

```python
    try:
        vision_res = vision_chain.invoke({
            "ctx": ctx,
            "instruction": ctx["instruction"],
            "target": ctx["target"],
            "prev": prev_path, "post": post_path
        })
    except Exception as e:
        vision_res = VisionObservation(
            prev_screen_elements="System error",
            post_screen_changes="System error",
            semantic_screen_summary="Vision observer crashed"
        )
```

Calls the Vision Observer via the LangChain runnable. If it crashes (GPU OOM, model error, etc.), produces a safe fallback observation instead of crashing the entire pipeline.

```python
    return {"_current_vision_obs": vision_res.model_dump()}
```

Writes the observation to transient state as a dictionary (`.model_dump()` converts a Pydantic model to a plain dict). The next node (log_node) and then decider_node will read this.

---

### Lines 117-128: Node 4 — log_node (Checks for System Logs)

```python
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
```

Constructs the expected log file path and calls the Log Observer. If the file doesn't exist, the Log Observer immediately returns `has_logs=False` without calling the LLM.

---

### Lines 134-162: Node 5 — decider_node (Calls the Logic AI)

```python
def decider_node(state: GraphState):
    ctx = state['_current_ctx']
    vision_obs_dict = state.get('_current_vision_obs', {})
    log_obs_dict = state.get('_current_log_obs', {})
    active_failures = list(state.get('active_failures', []))
```

Gathers all evidence: step context, vision observation, log observation, and failure history.

```python
    try:
        vision_obs = VisionObservation(**vision_obs_dict)
    except:
        vision_obs = VisionObservation()  # Use defaults if state is corrupted
```

Reconstructs the Pydantic objects from dictionaries (they were serialized to dicts for state transport).

```python
    try:
        final_res = logic_chain.invoke({
            "ctx": ctx,
            "vision_report": vision_obs,
            "log_report": log_obs,
            "active_failures": active_failures,
        })
    except Exception as e:
        final_res = FinalDecision(is_failure=True, failure_type="system_error",
                                  observation="System crash", root_cause=str(e))
```

Calls the Logic Decider with ALL available evidence. On crash, defaults to a failure verdict (conservative — it's better to report a false positive than silently miss a real bug).

---

### Lines 168-202: Node 6 — accumulate_node (Record Results)

```python
def accumulate_node(state: GraphState):
    idx = state.get('current_step_index', 0)
    errors = list(state.get('detected_errors', []))              # Copy (don't mutate original)
    active_failures = list(state.get('active_failures', []))     # Copy
    active_failure_map = dict(state.get('active_failure_map', {}))  # Copy
```

**Why all the `list()` and `dict()` copies?** LangGraph state is shared. If we appended directly to the original list, we'd mutate state in an unpredictable way. Always create fresh copies, modify them, and return the new versions.

```python
    gc.collect()                                        # Python garbage collection
    if torch.cuda.is_available(): torch.cuda.empty_cache()  # Free unused GPU memory
```

Housekeeping: reclaim any GPU memory used by intermediate tensors from the inference calls. The model weights stay loaded, but activation memory is freed.

```python
    if final_res.is_failure:
        # Cascade attribution: if this is a content_mismatch and there are prior failures,
        # link this failure to the most recent one
        if active_failure_map and final_res.failure_type == 'content_mismatch':
            most_recent_fail = max(active_failure_map.keys())
            if f"Step {most_recent_fail}" not in final_res.root_cause:
                final_res.root_cause += f" (Likely caused by Step {most_recent_fail})"

        reason = final_res.root_cause if final_res.root_cause else final_res.observation
        errors.append({"step_index": idx, "reason": reason})
        active_failures.append(f"Step {idx}: {reason}")
        active_failure_map[idx] = final_res.model_dump()
    else:
        print(f" PASSED: {final_res.observation}")
```

If the step failed:
- **Cascade attribution:** If this failure is a `content_mismatch` (wrong value seen) and prior failures exist, it's likely caused by an earlier step that went wrong. The system appends "(Likely caused by Step X)" to help trace the root cause chain.
- Records the failure in three places: `errors` (for the final report), `active_failures` (human-readable list for prompt injection), `active_failure_map` (structured data for cascade attribution).

```python
    return {
        "current_step_index": idx + 1,        # Advance to the next step
        "detected_errors": errors,
        "active_failures": active_failures,
        "active_failure_map": active_failure_map,
    }
```

Returns the updated state. The step counter increments, and the graph loops back to `router_node` for the next step.

---

### Lines 208-239: build_graph() — Assembling the Pipeline

```python
def build_graph():
    workflow = StateGraph(GraphState)
```

Creates a new graph with `GraphState` as its state type.

```python
    # Register all 6 nodes
    workflow.add_node("router", router_node)
    workflow.add_node("skip", skip_node)
    workflow.add_node("vision", vision_node)
    workflow.add_node("log", log_node)
    workflow.add_node("decider", decider_node)
    workflow.add_node("accumulate", accumulate_node)
```

Each node is a Python function registered under a string name.

```python
    workflow.set_entry_point("router")
```

The graph always starts at the router node.

```python
    # Router decides: end / skip / analyze
    workflow.add_conditional_edges("router", route_step, {
        "skip": "skip",
        "analyze": "vision",
        "end": END,
    })
```

**Conditional edges:** After the router runs, `route_step()` returns one of three strings. The dictionary maps each string to the next node. `END` is the special terminal node.

```python
    # Analysis pipeline: vision → log → decider → accumulate (sequential)
    workflow.add_edge("vision", "log")
    workflow.add_edge("log", "decider")
    workflow.add_edge("decider", "accumulate")
```

Fixed edges — no conditions. After vision, always go to log, then decider, then accumulate.

```python
    # Loop back to router from both skip and accumulate
    workflow.add_edge("skip", "router")
    workflow.add_edge("accumulate", "router")
```

After processing a step (whether skipped or analyzed), loop back to the router for the next step. This creates the main loop.

```python
    return workflow.compile()
```

`compile()` validates the graph (checks for unreachable nodes, missing edges) and returns an executable `CompiledGraph` object with an `.invoke()` method.

---

## 10. File 4: runner.py — The Entry Point

**Purpose:** The single Colab cell the user runs to execute the entire pipeline. Handles setup, imports, and invocation.

### Line 10: Install Dependencies

```python
!pip install -q -U "transformers==4.49.0" accelerate huggingface_hub \
    langchain langchain-core langgraph pydantic nest_asyncio \
    "qwen-vl-utils==0.0.8" torchvision
```

`!` is a Colab/Jupyter syntax for running shell commands. This installs all Python packages:
- `-q` = quiet (less output)
- `-U` = upgrade if already installed
- `transformers==4.49.0` — version pinned (newer versions break Qwen2.5-VL)
- `qwen-vl-utils==0.0.8` — pinned to Qwen's recommended version

### Lines 12-14: Async Patch

```python
import nest_asyncio
nest_asyncio.apply()
```

Patches Python's event loop. Colab runs its own async event loop; LangGraph also uses async internally. Without this, you get `RuntimeError: This event loop is already running`. `apply()` makes nested async calls legal.

### Lines 17-20: Path Setup

```python
PROJECT_PATH = "/content/drive/MyDrive/Grad Project"
STEPS_PATH = f"{PROJECT_PATH}/steps"
if PROJECT_PATH not in sys.path:
    sys.path.insert(0, PROJECT_PATH)
```

Adds the project folder to Python's import search path. Without this, `import models` would fail because Python wouldn't know where `models.py` lives.

### Lines 22-24: Import Project Modules

```python
from models import load_project_data
from agents import load_models
from graph import build_graph
```

Now that the project path is in `sys.path`, we can import our custom modules.

### Line 27: Load AI Models

```python
load_models()
```

Mounts Drive, authenticates Hugging Face, downloads/loads both AI models into GPU memory. This takes 2-5 minutes on first run (downloading ~42 GB), or ~30 seconds on subsequent runs (loading from Drive cache).

### Lines 30-31: Build the Graph

```python
app = build_graph()
```

Creates the compiled LangGraph pipeline. `app` is now an executable graph with an `.invoke()` method.

### Lines 33-52: Run the Pipeline

```python
steps_data = load_project_data(STEPS_PATH)
```

Loads `steps.json` into a Python list of dictionaries.

```python
if steps_data:
    res = app.invoke({
        "current_step_index": 0,          # Start from step 0
        "steps_data": steps_data,          # The full step list
        "trace_files_path": STEPS_PATH,    # Where screenshots live
        "final_report": "",                # Empty — will be built at the end
        "stop_execution": False,           # Don't stop yet
        "detected_errors": [],             # No errors found yet
        "active_failures": [],             # No failure history yet
        "active_failure_map": {},          # No failure details yet
        # Transient state — initialized to None
        "_current_ctx": None,
        "_current_vision_obs": None,
        "_current_log_obs": None,
        "_current_decision": None,
        "_route": None,
        "_skip_reason": None,
    }, config={"recursion_limit": 150})
```

**This is where the entire analysis runs.** `app.invoke()` starts the graph at the entry point (router) and keeps looping until `stop_execution=True`.

`recursion_limit=150` sets the maximum number of node executions. With 25 steps and ~4 nodes per step, we need ~100 executions. 150 gives comfortable headroom.

The initial state sets everything to zero/empty/None — a clean slate.

```python
    if "final_report" in res: print(res["final_report"])
```

After the graph finishes, the final state contains the complete report. Print it.

```python
except Exception as e:
    print(f"FATAL SYSTEM CRASH: {e}")
```

Last-resort error handler. If anything uncaught goes wrong (GPU OOM, disk full, etc.), we get a readable error instead of a raw Python traceback.

---

## 11. End-to-End Walkthrough — What Happens When You Press Run

Here's what happens for a single step (say Step 7: "Click on Nereden"):

1. **runner.py** installs packages, loads models (~42 GB into GPU), builds the graph.

2. **router_node** reads step 7 from `steps.json`, extracts context: `action="click"`, `target="Nereden"`, `instruction="Nereden'e tıkla."` Images exist → route = "analyze".

3. **vision_node** loads `step_7_prev.png` (search form with "Nereden" visible) and `step_7_post.png` (airport picker opened). The Vision AI (Qwen2.5-VL-7B) produces:
   ```
   prev_screen_elements: "Target 'Nereden' is visible as a green text field..."
   post_screen_changes: "Screen shows airport selection. 'Nereden:' in search bar..."
   semantic_screen_summary: "The app navigated to the departure airport picker."
   ```

4. **log_node** checks for `step_7_logs.json` — doesn't exist → returns `has_logs=False`.

5. **decider_node** receives the text observation (never sees the images). The Logic AI (Qwen2.5-14B) applies rules:
   - Rule 0 (overlay)? No system dialog. Skip.
   - Rule 1 (target absent)? "Nereden" found in PREV. Navigation detected. → NAVIGATION EXCEPTION → SUCCESS.
   - Output: `is_failure=false`, `failure_type="none"`.

6. **accumulate_node** prints "PASSED", advances counter to step 8.

7. **Back to router_node** for step 8. Repeat until all 25 steps are processed.

8. **Final router_node** call: `idx >= len(steps)` → build report → `stop_execution=True` → END.

---

*Document prepared for professor meeting — Describer-Decider Engine v32.3*
