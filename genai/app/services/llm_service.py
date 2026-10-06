"""
Groq LLM Service
Handles chat completions, RAG-grounded failure diagnostics & auto-fix, and execution comparisons.
"""

import json
import re
from typing import Dict, Any, List
from groq import AsyncGroq
from app.config.settings import settings, logger
from app.schemas.request import BotRequest, FailureAssistRequest, CompareRequest
from app.services.rag_service import rag_memory_store

groq_client = AsyncGroq(api_key=settings.GROQ_API_KEY)

ACTIVE_MODELS = [
    "llama-3.1-8b-instant",
    "llama3-8b-8192",
    "qwen/qwen3.8-27b",
    "groq/compound",
    "openai/gpt-oss-120b"
]

async def call_groq_with_fallback(
    messages: List[Dict[str, str]],
    temperature: float = 0.2,
    max_tokens: int = 500,
    is_json: bool = False
) -> str:
    """Executes chat completion with Groq using automatic fallback models."""
    last_err = None
    for model in ACTIVE_MODELS:
        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens
            }
            if is_json:
                kwargs["response_format"] = {"type": "json_object"}
            res = await groq_client.chat.completions.create(**kwargs)
            content = res.choices[0].message.content
            if content and content.strip():
                return content
        except Exception as e:
            logger.warning(f"Groq model {model} failed: {e}. Trying fallback...")
            last_err = e

    raise last_err or RuntimeError("All Groq models failed.")


def extract_json_from_llm(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                pass
    return {}


# ============================================================================
# 🔹 Bot Response
# ============================================================================

BOT_SYSTEM_PROMPT = """You are J.A.R.V.I.S. 🤖 — an expert API Testing and Backend Development assistant for SwiftAPI.
Assist with REST APIs, HTTP methods, headers, parameters, authentication (JWT, OAuth, Bearer), status codes, JSON/XML, and backend debugging.
Keep responses concise, friendly, beginner-friendly (2-4 short paragraphs), and technically accurate. Use emojis sparingly."""

async def generate_bot_response(req: BotRequest) -> dict:
    ctx = req.currentApiContext or {}
    ctx_str = f"\nAPI Context: {ctx.get('method', '')} {ctx.get('url', '')} Status: {ctx.get('status', '')}" if ctx else ""
    messages = [{"role": "system", "content": BOT_SYSTEM_PROMPT}]

    for msg in (req.requestHistory or []):
        text = msg.get("text", "")
        if text and "Hi 👋" not in text:
            messages.append({"role": "user" if msg.get("from") == "user" else "assistant", "content": text})

    messages.append({"role": "user", "content": f"{req.message}{ctx_str}"})

    try:
        reply = await call_groq_with_fallback(messages, temperature=0.5, max_tokens=500)
        return {"type": "bot_response", "text": reply}
    except Exception as e:
        logger.error(f"Bot error: {e}")
        return {"type": "bot_response", "text": "❌ An error occurred while generating a response. Please try again."}


# ============================================================================
# 🔹 Failure Diagnosis & Auto-Fix
# ============================================================================

RAG_FAILURE_PROMPT = """You are SwiftAPI's History-Grounded Diagnostics Engine 🤖🛠️.
Diagnose this failed HTTP request, predict the backend failure layer, and produce an actionable autoFix.
Keep whatHappened and why STRICTLY 1 concise sentence each so developers can scan quickly.
Place all detailed troubleshooting in whatToDo.
Output ONLY valid JSON matching this schema:
{
  "whatHappened": "Crisp 1-sentence description of what failed.",
  "why": "Crisp 1-sentence explanation of why it failed.",
  "evidence": ["Evidence point 1", "Evidence point 2"],
  "whatToDo": ["Specific debug action 1", "Specific debug action 2"],
  "rootCause": {
    "predictedLayer": "Database | JWT / Authentication | Authorization | Validation | Server / Business Logic | Network | Configuration",
    "confidence": 85,
    "probableCause": "Summary of cause within this layer",
    "evidenceSummary": "Signals supporting layer",
    "nextAction": "Action to fix",
    "isPrediction": true
  },
  "autoFix": {
    "fixable": true,
    "fixType": "url | header | auth | body | param | method",
    "title": "Short title of fix",
    "description": "What this fix will change",
    "confirmationPrompt": "Confirmation message",
    "diff": "Clean diff or payload snippet",
    "actionPayload": { "type": "set_url | add_header | set_auth | fix_body | change_method", "key": "url", "value": "" }
  },
  "historyEvolutionInsight": "Brief note comparing with past history or RAG memory."
}"""


def _build_default_fix(req: FailureAssistRequest, retrieved: List[dict]) -> dict:
    status_num = int(req.status) if str(req.status).isdigit() else 500
    url = req.url or ""

    dec_match = re.search(r"\/(\d+)\.\d+", url)
    if dec_match:
        fixed_url = re.sub(r"\/(\d+)\.\d+", r"/\1", url)
        return {
            "fixable": True, "fixType": "url", "title": "Correct Resource ID to Integer",
            "description": f"Convert decimal ID in URL to integer ({dec_match.group(1)})",
            "confirmationPrompt": f"Update URL to '{fixed_url}'?", "diff": f"- {url}\n+ {fixed_url}",
            "actionPayload": {"type": "set_url", "key": "url", "value": fixed_url}
        }

    if retrieved and retrieved[0].get("successfulFixUsed"):
        rf = retrieved[0]["successfulFixUsed"]
        return {
            "fixable": True, "fixType": rf.get("fixType", "url"),
            "title": rf.get("title", "Apply Verified Historical Fix"),
            "description": rf.get("description", "Apply proven fix from RAG memory"),
            "confirmationPrompt": "Apply proven fix from history?", "diff": rf.get("diff", "+ Applied from past run"),
            "actionPayload": rf.get("actionPayload", {"type": "set_url", "key": "url", "value": url})
        }

    if status_num == 401:
        return {
            "fixable": True, "fixType": "auth", "title": "Configure Bearer Token",
            "description": "Add Authorization Bearer token to request headers.",
            "confirmationPrompt": "Configure Authorization token?", "diff": "+ Authorization: Bearer <token>",
            "actionPayload": {"type": "set_auth", "authType": "bearer", "requiresUserInput": True, "userInputPrompt": "Enter Bearer Token"}
        }

    if status_num == 404:
        for typo, fix in [("commentss", "comments"), ("postss", "posts"), ("todoss", "todos")]:
            if typo in url:
                fixed_url = url.replace(typo, fix)
                return {
                    "fixable": True, "fixType": "url", "title": "Correct URL Typo",
                    "description": f"Fixed trailing typo in endpoint path: {fix}",
                    "confirmationPrompt": f"Update URL to '{fixed_url}'?", "diff": f"- {url}\n+ {fixed_url}",
                    "actionPayload": {"type": "set_url", "key": "url", "value": fixed_url}
                }

    return {
        "fixable": True, "fixType": "url", "title": "Review Request Parameters",
        "description": "Check headers, parameters, and endpoint configuration.",
        "confirmationPrompt": "Inspect current request configuration?", "diff": f"Target: {url}",
        "actionPayload": {"type": "set_url", "key": "url", "value": url}
    }


async def generate_failure_diagnosis(req: FailureAssistRequest) -> dict:
    retrieved = rag_memory_store.retrieve_relevant_episodes(
        user_id=req.userId or "guest",
        method=req.method,
        url=req.url,
        status=req.status,
        error_text=str(req.response or "")[:200],
        headers_keys=list(req.headers.keys()) if isinstance(req.headers, dict) else [],
        top_k=2
    )

    rag_context = "\n".join([
        f"Episode: {ep['endpoint']} -> {ep['failedStatus']} (Layer: {ep['rootCauseLayer']}) Fix: {json.dumps(ep['successfulFixUsed'])}"
        for ep in retrieved
    ]) if retrieved else "No previous episodes found."

    user_prompt = f"""Failed Request:
Method: {req.method} | URL: {req.url} | Status: {req.status} | Duration: {req.duration}ms
Headers: {json.dumps(req.headers or {})}
Body: {json.dumps(req.body) if req.body else 'None'}
Response: {json.dumps(req.response) if isinstance(req.response, (dict, list)) else str(req.response or '')[:300]}
RAG Evidence: {rag_context}"""

    try:
        content = await call_groq_with_fallback(
            [{"role": "system", "content": RAG_FAILURE_PROMPT}, {"role": "user", "content": user_prompt}],
            temperature=0.2, max_tokens=550, is_json=True
        )
        diag = extract_json_from_llm(content)
        if not diag or "whatHappened" not in diag:
            diag = {
                "whatHappened": f"Request failed with status {req.status}.",
                "why": "Target server rejected the request.",
                "evidence": [f"Status code {req.status}"],
                "whatToDo": ["Check request headers, URL, and payload."],
                "rootCause": {
                    "predictedLayer": "Server / Business Logic", "confidence": 80,
                    "probableCause": f"Status {req.status} response from server.",
                    "evidenceSummary": f"HTTP {req.status}", "nextAction": "Review request.",
                    "isPrediction": True
                }
            }

        if not diag.get("autoFix") or not diag["autoFix"].get("actionPayload"):
            diag["autoFix"] = _build_default_fix(req, retrieved)

        return {"success": True, "diagnosis": diag, "retrievedEpisodes": retrieved}

    except Exception as e:
        logger.error(f"Diagnosis error: {e}")
        return {
            "success": False, "error": str(e),
            "diagnosis": {
                "whatHappened": f"Request failed with status {req.status}.",
                "why": "AI diagnostics encountered an error.",
                "evidence": [f"Status: {req.status}"],
                "whatToDo": ["Check request manually."],
                "rootCause": {"predictedLayer": "Server / Business Logic", "confidence": 70, "probableCause": "Backend error.", "evidenceSummary": f"Status: {req.status}", "nextAction": "Review logs.", "isPrediction": True},
                "autoFix": _build_default_fix(req, retrieved)
            },
            "retrievedEpisodes": retrieved
        }


# ============================================================================
# 🔹 History Comparison
# ============================================================================

COMPARE_SYSTEM_PROMPT = """Compare two API attempts (Attempt A vs B). Return ONLY valid JSON:
{
  "statusComparison": { "attemptAStatus": "401", "attemptBStatus": "200", "statusChanged": true, "summary": "" },
  "timingComparison": { "attemptADuration": 400, "attemptBDuration": 100, "differenceMs": -300, "insight": "" },
  "detectedChanges": [{ "field": "", "attemptA": "", "attemptB": "", "impact": "" }],
  "aiExplanation": "Clear summary explaining why the outcome changed."
}"""

def _build_fallback_comparison(a: dict, b: dict) -> dict:
    st_a, st_b = str(a.get("status", "ERR")), str(b.get("status", "ERR"))
    dur_a, dur_b = int(a.get("duration") or 0), int(b.get("duration") or 0)
    diff = dur_b - dur_a

    changes = []
    if a.get("url") != b.get("url"):
        changes.append({"field": "URL Endpoint", "attemptA": str(a.get("url")), "attemptB": str(b.get("url")), "impact": "Endpoint updated."})
    if a.get("method") != b.get("method"):
        changes.append({"field": "HTTP Method", "attemptA": str(a.get("method")), "attemptB": str(b.get("method")), "impact": "Method changed."})

    return {
        "statusComparison": {"attemptAStatus": st_a, "attemptBStatus": st_b, "statusChanged": st_a != st_b, "summary": f"Status changed from {st_a} to {st_b}."},
        "timingComparison": {"attemptADuration": dur_a, "attemptBDuration": dur_b, "differenceMs": diff, "insight": f"Attempt B was {abs(diff)}ms {'faster' if diff < 0 else 'slower'}."},
        "detectedChanges": changes,
        "aiExplanation": f"Attempt A returned status {st_a}, while Attempt B returned status {st_b} with updated parameters."
    }

async def generate_history_comparison(req: CompareRequest) -> dict:
    a, b = req.attemptA, req.attemptB
    fallback = _build_fallback_comparison(a, b)

    prompt = f"""Attempt A: {a.get('method')} {a.get('url')} -> Status {a.get('status')} ({a.get('duration')}ms)
Headers: {json.dumps(a.get('headers') or {})} | Body: {json.dumps(a.get('body') or '')}
Attempt B: {b.get('method')} {b.get('url')} -> Status {b.get('status')} ({b.get('duration')}ms)
Headers: {json.dumps(b.get('headers') or {})} | Body: {json.dumps(b.get('body') or '')}"""

    try:
        content = await call_groq_with_fallback(
            [{"role": "system", "content": COMPARE_SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=450, is_json=True
        )
        data = extract_json_from_llm(content)
        return {"success": True, "comparison": data if data and "aiExplanation" in data else fallback}
    except Exception as e:
        logger.error(f"Comparison error: {e}")
        return {"success": True, "comparison": fallback}