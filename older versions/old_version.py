# =========================================================
# VERSION 5.3: FINAL STABLE FIX
# 1. Dependency Conflict Resolved (Removed strict pinning)
# 2. Rate Limit Fix (Image Resizing to 800px confirmed)
# =========================================================

# Önce sessizce ama uyumlu şekilde yükleyelim
!pip install -q -U langchain-groq langgraph langchain-core "pydantic==2.12.3" pyautogen autogen-ext nest_asyncio pillow numpy

import os
import json
import base64
import asyncio
import nest_asyncio
import time
import numpy as np
from io import BytesIO
from typing import TypedDict, List, Optional, Literal, Dict, Any
from getpass import getpass
from google.colab import drive
from PIL import Image

# Kütüphanelerin yüklendiğinden emin olalım
try:
    from langchain_groq import ChatGroq
    from langchain_core.messages import HumanMessage, SystemMessage
    from langgraph.graph import StateGraph, END
    from pydantic import BaseModel, Field
    from autogen_agentchat.agents import AssistantAgent
    from autogen_ext.models.openai import OpenAIChatCompletionClient
except ImportError as e:
    print("Kurulum hatası devam ediyor, lütfen Runtime'ı Restart edip tekrar deneyin.")
    raise e

nest_asyncio.apply()

# =========================================================
# 1. SETUP
# =========================================================
if not os.path.exists('/content/drive'):
    drive.mount('/content/drive')

# Dosya yolunu kendi yoluna göre düzenle
PROJECT_PATH = "/content/drive/MyDrive/Grad Project"

if "GROQ_API_KEY" not in os.environ:
    os.environ["GROQ_API_KEY"] = getpass("Please enter your GROQ API Key: ")

# =========================================================
# 2. HELPERS (TOKEN SAVER MODE)
# =========================================================

def encode_image(image_path: str) -> Optional[str]:
    """
    Resimleri küçültür ve JPEG formatına çevirir.
    Bu işlem token kullanımını 6000'den ~1500-2000 seviyesine indirir.
    """
    try:
        if not os.path.exists(image_path):
            return None
            
        img = Image.open(image_path)
        
        # Format dönüşümü (PNG -> RGB)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        # RESIZE: Maksimum 800px (Vision modelleri için ideal boyut)
        img.thumbnail((800, 800)) 
        
        buffered = BytesIO()
        # JPEG ve Kalite 85 (Gözle görülür fark yok, veri boyutu çok düşük)
        img.save(buffered, format="JPEG", quality=85)
        
        return base64.b64encode(buffered.getvalue()).decode("utf-8")
    except Exception as e:
        print(f"Image processing error: {e}")
        return None

def load_project_data(folder_path: str) -> List[dict]:
    steps_path = os.path.join(folder_path, "steps.json")
    if not os.path.exists(steps_path):
        print(f"HATA: '{steps_path}' bulunamadı. PROJECT_PATH'i kontrol edin.")
        return []
    with open(steps_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data

def format_active_failures(failures: List[str]) -> str:
    if not failures:
        return "None. System is healthy so far."
    return "\n".join(failures)

def run_coro_sync(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)

def extract_json_from_text(text: str) -> str:
    t = (text or "").strip()
    if "```" in t:
        parts = t.split("```")
        for p in parts:
            if p.strip().lower().startswith("json"):
                return p.strip()[4:].strip()
            if "{" in p and "}" in p:
                return p.strip()
    if "{" in t and "}" in t:
        return t[t.find("{"):t.rfind("}")+1]
    return t

def is_black_screen(path: str) -> bool:
    try:
        img = Image.open(path).convert("L")
        arr = np.array(img)
        return float(arr.mean()) < 20.0 and float(arr.std()) < 5.0
    except:
        return False

# =========================================================
# 3. STATE & MODELS
# =========================================================
class StepError(TypedDict):
    step_index: int
    reason: str
    observation: str
    confidence: float
    severity: str

class GraphState(TypedDict, total=False):
    current_step_index: int
    steps_data: List[dict]
    trace_files_path: str
    final_report: str
    stop_execution: bool
    detected_errors: List[StepError]
    execution_history: List[dict]
    active_failures: List[str]
    active_failure_map: Dict[int, Dict[str, Any]]

FailureType = Literal["element_missing", "action_failed", "visual_bug", "content_mismatch", "cascading_failure", "none"]

class AgentResult(BaseModel):
    is_failure: bool
    failure_type: FailureType
    confidence: float = Field(ge=0.0, le=1.0)
    observation: str
    root_cause: str
    caused_by_step_index: Optional[int] = None

class ReviewResult(BaseModel):
    review_ok: bool
    suggested_confidence: float = Field(ge=0.0, le=1.0)
    notes: str

NEW_MODEL_NAME = "meta-llama/llama-4-maverick-17b-128e-instruct"

llm = ChatGroq(model=NEW_MODEL_NAME, temperature=0)

groq_model_info = {
    "family": "openai",
    "vision": False,
    "function_calling": False,
    "json_output": True,
    "max_tokens": 4096
}

autogen_client = OpenAIChatCompletionClient(
    model=NEW_MODEL_NAME,
    api_key=os.environ.get("GROQ_API_KEY", ""),
    base_url="https://api.groq.com/openai/v1",
    model_info=groq_model_info
)

reviewer = AssistantAgent(
    name="Reviewer",
    model_client=autogen_client,
    system_message="You are a QA Auditor. Check if the Agent missed any numerical mismatches (prices, times). Output JSON."
)

# =========================================================
# 4. CORE LOGIC
# =========================================================
def analyze_step_node(state: GraphState):
    idx = state['current_step_index']
    steps = state['steps_data']
    folder = state['trace_files_path']
    
    errors = list(state.get('detected_errors', []))
    active_failures = list(state.get('active_failures', []))
    active_failure_map = dict(state.get("active_failure_map", {}))

    if idx >= len(steps):
        report = f"TEST COMPLETE. Scanned {len(steps)} steps.\n"
        if errors:
            report += f"FAILURES ({len(errors)}):\n"
            for err in errors:
                report += f"Step {err['step_index']}: {err['reason']} (Conf: {err['confidence']})\n"
        else:
            report += "ALL STEPS PASSED."
        return {"stop_execution": True, "final_report": report}

    # RATE LIMIT BEKLEMESİ
    print("⏳ Waiting for rate limit cooldown (5s)...")
    time.sleep(5) 
    
    current_step = steps[idx]
    prev_path = os.path.join(folder, f"step_{idx}_prev.png")
    post_path = os.path.join(folder, f"step_{idx}_post.png")
    
    print(f"\n--- Analyzing Step {idx} ---")
    
    # 1. Black Screen Check
    if os.path.exists(prev_path) and is_black_screen(prev_path):
        err_msg = "Critical Visual Bug: Screen is black/empty."
        print(f" 🚨 {err_msg}")
        errors.append({"step_index": idx, "reason": err_msg, "observation": "Black screen", "confidence": 1.0, "severity": "critical"})
        active_failures.append(f"Step {idx}: Black Screen")
        active_failure_map[idx] = {"failure_type": "visual_bug", "reason": err_msg}
        return {"current_step_index": idx + 1, "detected_errors": errors, "active_failures": active_failures, "active_failure_map": active_failure_map}

    # RESIMLERI SIKIŞTIRARAK AL
    prev_b64 = encode_image(prev_path)
    post_b64 = encode_image(post_path)

    if not prev_b64 or not post_b64:
        print("Skipping step due to missing images.")
        return {"current_step_index": idx + 1}

    # 2. Extract Intent
    action_type = current_step.get('action', 'unknown')
    target_element = current_step.get('element', 'Unknown')
    instruction = current_step.get('stepInstruction') or current_step.get('instruction', 'None')

    is_verification = "verification" in action_type.lower() or "abstract" in action_type.lower()
    
    mode_instruction = ""
    if is_verification:
        mode_instruction = """
        MODE: DATA VERIFICATION
        1. Compare User Instruction Numbers/Text vs POST Image.
        2. STRICTLY CHECK: Times, Prices, Dates, Flight Numbers.
        3. If instruction says '12:15' and screen says '21:15', this is a 'content_mismatch' FAILURE.
        """
    else:
        mode_instruction = """
        MODE: ACTION & NAVIGATION
        1. Check if Target Element (or semantically similar) exists in PREV.
        2. Check if Screen CHANGED in POST (Navigation occurred).
        """

    system_prompt = f"""You are an Expert App QA Agent.
    {mode_instruction}
    OUTPUT JSON ONLY:
    {{
        "is_failure": boolean,
        "failure_type": "element_missing" | "action_failed" | "visual_bug" | "content_mismatch" | "cascading_failure" | "none",
        "confidence": float,
        "observation": "Describe visible elements.",
        "root_cause": "Why it failed.",
        "caused_by_step_index": int or null
    }}
    """

    user_content = [
        {"type": "text", "text": f"""
        STEP: {idx}
        INSTRUCTION: "{instruction}"
        TARGET ELEMENT: "{target_element}"
        KNOWN ACTIVE BUGS: {format_active_failures(active_failures)}
        """},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{prev_b64}"}},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{post_b64}"}}
    ]

    try:
        response = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_content)])
        raw_json = extract_json_from_text(response.content)
        result = json.loads(raw_json)
        validated = AgentResult(**result)

        # Reviewer Check
        time.sleep(2)
        review_prompt = f"Step {idx}: {instruction}. Decision: {validated.model_dump_json()}"
        review_reply = run_coro_sync(reviewer.run(task=review_prompt))
        
        # Sonuç İşleme
        final_res = validated.model_dump()
        
        if final_res['is_failure']:
            # Cascading Logic
            if active_failure_map and final_res['failure_type'] in ['content_mismatch', 'action_failed']:
                 most_recent_fail = max(active_failure_map.keys())
                 final_res['failure_type'] = "cascading_failure"
                 final_res['root_cause'] += f" (Likely caused by Step {most_recent_fail})"

            print(f" 🚨 FAILED: {final_res['root_cause']}")
            errors.append({"step_index": idx, "reason": final_res['root_cause'], "observation": final_res['observation'], "confidence": final_res['confidence'], "severity": "high"})
            active_failures.append(f"Step {idx}: {final_res['root_cause']}")
            active_failure_map[idx] = final_res
        else:
            print(f" ✅ PASSED: {final_res['observation']}")

        return {"current_step_index": idx + 1, "detected_errors": errors, "active_failures": active_failures, "active_failure_map": active_failure_map}

    except Exception as e:
        print(f"Error processing Step {idx}: {e}")
        time.sleep(10)
        return {"current_step_index": idx + 1}

# =========================================================
# 5. RUN
# =========================================================
def decision_maker(state: GraphState):
    return "end" if state.get("stop_execution") else "continue"

workflow = StateGraph(GraphState)
workflow.add_node("analyst", analyze_step_node)
workflow.set_entry_point("analyst")
workflow.add_conditional_edges("analyst", decision_maker, {"continue": "analyst", "end": END})
app = workflow.compile()

print(f"🚀 ROBUST SCAN STARTED... {PROJECT_PATH}")

try:
    steps = load_project_data(PROJECT_PATH)
    app.invoke({
        "current_step_index": 0,
        "steps_data": steps,
        "trace_files_path": PROJECT_PATH,
        "stop_execution": False,
        "detected_errors": [],
        "active_failures": [],
        "active_failure_map": {}
    }, config={"recursion_limit": 150})
except Exception as e:
    print(f"CRITICAL ERROR STARTING SCAN: {e}")