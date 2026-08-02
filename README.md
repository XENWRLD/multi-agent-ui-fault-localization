# AI-Powered Fault Localization Using Vision-Language Models

**Describer-Decider Engine** — a multi-agent AI pipeline that automatically diagnoses failures in UI-driven applications (web & mobile) by reasoning over before/after screenshots and execution logs.

> Graduation Project — Sabancı University, Computer Science

---

## Overview

Traditional automated UI testing tools produce a binary pass/fail outcome and stop at the first failure, leaving engineers to manually dig through logs and screenshots to find the root cause. **Describer-Decider** removes that manual step.

Given a sequence of test steps (screenshots + execution logs), the system:

1. Determines whether **each individual step** passed or failed
2. When a failure is found, identifies its **root cause**
3. Links related failures into **causal chains** — distinguishing the true root failure from its downstream symptoms
4. Produces a structured, human-readable diagnosis report

The system requires **no application source code** — only screenshots, step descriptors, and (optionally) execution logs.

## Key Results

Evaluated on **12 applications / 136 execution steps** across two independent datasets:

| Dataset | Apps | Steps | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|---|
| Proprietary traces | 7 | 90 | 12 | 0 | 0 | 100.0% | 100.0% | **100.0%** |
| External (Group 411) | 5 | 46 | 11 | 1 | 2 | 91.7% | 84.6% | **88.0%** |
| **Combined** | **12** | **136** | **23** | **1** | **2** | **95.8%** | **92.0%** | **93.9%** |

## Architecture

A four-agent, six-node pipeline built on **GPT-4o / GPT-4o-mini**, orchestrated with **LangGraph**:

```
Input (steps.json / fail.json + images + log.json)
 │
 │ [input_adapter.py] → format detection (v33 / G411) + normalization
 ▼
 [Router] ── skip ──→ [Skip Node] (non-VLM steps, wait actions, missing images)
 │
 └── analyze ──→ [Vision Observer] (GPT-4o, PREV + POST screenshots)
                        │
                  [Log Observer] (GPT-4o-mini, sliced log entries)
                        │
                  [Logic Decider] (GPT-4o, fuses visual + log evidence)
                        │
                  [Accumulate] (confidence adjustment, causal linking)
                        │
                    (loop back to Router)
                        │
              [Root Cause Analysis] (GPT-4o, cross-step retrospective)
                        │
              [Final Diagnosis Report]
```

### The Four Agents

| Agent | Model | Role |
|---|---|---|
| **Vision Observer** | GPT-4o | Describes visual changes between PREV/POST screenshots — makes **no** pass/fail judgment |
| **Log Observer** | GPT-4o-mini | Parses execution logs into structured error/network/latency flags |
| **Logic Decider** | GPT-4o | Fuses visual + log evidence into a pass/fail verdict with confidence score |
| **Root Cause Agent** | GPT-4o | Post-run retrospective across *all* failures — finds long-gap causal chains the mechanical scorer misses |

Separating observation from judgment (Vision Observer never decides pass/fail) prevents anchoring bias in the Logic Decider.

## Why This Is Hard: Causal Chain Detection

A single root failure often produces several downstream symptoms. The system detects these chains two ways:

- **Mechanically** (`find_likely_cause`) — scores candidate upstream causes using element-text overlap, a failure-type correlation matrix, and temporal proximity decay.
- **With an LLM** (`run_root_cause_agent`) — reasons across the *entire* run at once, catching long-gap semantic chains (e.g. a wrong item selected at step 7 that only gets confirmed as wrong at step 14) that the mechanical scorer's decay threshold misses.

Example report output:

```
ROOT CAUSE CHAIN ANALYSIS
Root: Step 7 [content_mismatch | conf: 0.95]
 └─ Step 8  [dist: 1 hop ] Verification confirms 21:15 not 12:15...
 └─ Step 14 [dist: 2 hops] Checkout order shows wrong pizza...

Corrupted state: Selected flight item in session (wrong flight stored after Step 7)
Investigate first: The flight selection click handler
```

## Supported Input Formats

The system auto-detects and normalizes two trace formats:

- **(native)** — flat JSON array, disk-based screenshots (`step_N_prev.png` / `step_N_post.png`)
- **(external)** — nested format with inline base64 screenshots, used by the external Group 411 benchmark cases

It also unifies **four different log shapes** (`nested`, `flat_indexed`, `flat_assertions`, `raw`) into a single internal representation via automatic format detection.

## Repository Structure

```
├── src/                     # Core pipeline code
│   ├── agents.py            # 4 AI agents (OpenAI API calls, retry logic)
│   ├── graph.py              # LangGraph 6-node StateGraph orchestration
│   ├── input_adapter.py      # Format detection & normalization (v33 / G411)
│   ├── models.py             # Pydantic models, GraphState, causal-chain logic
│   ├── runner.py             # Colab entry point
│   └── parser/
│       └── log_parser.py     # Unified log ingestion (4 log format parsers)
│
├── data/
│   ├── test-cases/           # External Group 411 benchmark cases (5 apps)
│   └── real-world-apps/      # Proprietary evaluation traces (7 apps)
│
├── docs/
│   └── documentation/        # Full project report, presentation, system docs
│
└── results/
    └── latest/                # Evaluation outputs per app
```

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | Colab compatibility |
| Data validation | Pydantic v2 | All agent I/O strictly typed |
| Orchestration | LangGraph (≥0.2,<0.3) | Deterministic, auditable stateful graph — chosen over AutoGen for reproducibility |
| Vision model | GPT-4o | Multimodal OCR, no GPU required |
| Reasoning model | GPT-4o | Text-only decision logic + root cause analysis |
| Log extraction | GPT-4o-mini | ~10× cheaper, sufficient for structured extraction |
| Runtime | Google Colab | CPU-only, no local setup needed |

No `torch`, `transformers`, or GPU dependencies — the entire pipeline runs on API calls.

## Running It

The project is designed to run in Google Colab:

```bash
pip install openai langchain langchain-core "langgraph>=0.2,<0.3" pydantic nest_asyncio
```

```python
# in src/runner.py
PROJECT_PATH = "/content/drive/MyDrive/Grad Project"
# set OPENAI_API_KEY when prompted, then:
steps_data = load_project_data(STEPS_PATH)
parsed_logs = parse_log_file(f"{STEPS_PATH}/log.json", total_steps=len(steps_data))
set_log_source(parsed_logs)
result = app.invoke({...})  # see src/runner.py for full initial state
```


Full write-up of these and other findings is in [`docs/documentation`](./docs/documentation).

## Full Report

For the complete technical write-up — architecture rationale, prompt design methodology, per-application evaluation breakdown, and error analysis — see the [full project report](./docs/documentation) in `docs/documentation`.
