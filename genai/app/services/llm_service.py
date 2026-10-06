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
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b"
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


import difflib
from urllib.parse import urlparse

# ============================================================================
# 🔹 Failure Diagnosis & Auto-Fix
# ============================================================================

def is_similar_url_typo(current_url: str, past_url: str) -> bool:
    """
    Returns True ONLY if current_url and past_url are on the same host,
    share the same route segment count, and differ by a slight typo (similarity > 0.72)
    in exactly one non-numeric segment.
    """
    if not current_url or not past_url or current_url.strip() == past_url.strip():
        return False
    try:
        p1 = urlparse(current_url)
        p2 = urlparse(past_url)
        if p1.netloc.lower() != p2.netloc.lower():
            return False

        s1 = [s for s in p1.path.strip("/").split("/") if s]
        s2 = [s for s in p2.path.strip("/").split("/") if s]
        if len(s1) != len(s2) or len(s1) == 0:
            return False

        diff_count = 0
        for seg1, seg2 in zip(s1, s2):
            if seg1 != seg2:
                if seg1.isdigit() and seg2.isdigit():
                    return False
                diff_count += 1
                ratio = difflib.SequenceMatcher(None, seg1.lower(), seg2.lower()).ratio()
                if ratio < 0.72:
                    return False

        return diff_count == 1
    except Exception:
        return False


RAG_FAILURE_PROMPT = """You are SwiftAPI's Intelligent Diagnostics Engine 🤖🛠️.
Analyze the failed HTTP request using ALL provided context: HTTP method, URL, status code, response body, error messages/stack traces, headers, session history, and RAG evidence.

DIAGNOSTIC ACCURACY RULES:
1. Root Cause Classification:
   - 401 Unauthorized / TokenExpiredError / invalid token / missing token:
     * Layer: "JWT / Authentication"
     * why: Explain the exact token/authentication failure (e.g., "The JWT token in the Authorization header has expired." or "Authorization token is missing or invalid.").
     * autoFix: fixType "auth", title "Provide Valid Bearer Token", actionPayload {"type": "set_auth", "authType": "bearer", "requiresUserInput": true, "userInputPrompt": "Enter valid Bearer Token or re-login"}.
     * NEVER suggest URL changes or claim a URL typo for 401 / Auth errors!
   - 403 Forbidden:
     * Layer: "Authorization"
     * User is authenticated but lacks required role or permissions.
   - 404 Not Found:
     * Layer: "Server / Business Logic" or "Validation"
     * Check URL path. If "Relevant Past Session Attempts" shows a past 200 OK request on the SAME endpoint with a minor typo in the URL (e.g. 'Onkar-Satal' vs proven 'Onkar-Satale'), identify the typo and set autoFix to that proven URL.
     * If there is no typo, state that the route/resource does not exist on the server.
   - 400 / 422 Bad Request:
     * Layer: "Validation"
     * Missing or malformed payload fields, invalid types, or constraint violations.
   - 500 / 502 / 503 / 504:
     * Layer: "Server / Business Logic" or "Database" or "Network"
     * Unhandled exception, database error, or downstream service failure. Cite the stack trace if present.

2. Brevity & Style:
   - "whatHappened": Exactly 1 concise, direct sentence describing the failure.
   - "why": Exactly 1 concise, direct sentence stating the EXACT reason based on the response error and context.
   - "evidence": 2 to 3 short bullet points from the response message, stack trace, status, or headers.
   - "whatToDo": 2 to 3 specific, actionable steps to resolve the failure.

3. Auto-Fix (MANDATORY):
   - "autoFix" must correspond directly to the root cause:
     * Auth: fixType "auth", title "Configure Bearer Token", actionPayload {"type": "set_auth", "authType": "bearer", "requiresUserInput": true, "userInputPrompt": "Enter Bearer Token"}
     * URL typo: fixType "url", title "Correct URL Typo", actionPayload {"type": "set_url", "key": "url", "value": "<corrected_url>"}
     * Header: fixType "header", title "Add Missing Header", actionPayload {"type": "add_header", "key": "<Header-Name>", "value": "<Header-Value>"}
     * Body: fixType "body", title "Correct Request Body", actionPayload {"type": "fix_body", "value": <corrected_json>}

Output ONLY valid JSON matching this schema:
{
  "whatHappened": "Crisp 1-sentence description of what failed.",
  "why": "Crisp 1-sentence explanation of the exact reason why it failed.",
  "evidence": ["Evidence point 1", "Evidence point 2"],
  "whatToDo": ["Specific debug action 1", "Specific debug action 2"],
  "rootCause": {
    "predictedLayer": "Database | JWT / Authentication | Authorization | Validation | Server / Business Logic | Network | Configuration",
    "confidence": 95,
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
    resp_text = str(req.response or "").lower()

    # 1. Auth errors (401, or mentions token / expired / unauthorized)
    if status_num == 401 or "token" in resp_text or "unauthorized" in resp_text or "jwt" in resp_text:
        return {
            "fixable": True,
            "fixType": "auth",
            "title": "Configure Bearer Token",
            "description": "Provide a valid Authorization Bearer token or re-authenticate via login.",
            "confirmationPrompt": "Configure Authorization header?",
            "diff": "+ Authorization: Bearer <valid_token>",
            "actionPayload": {
                "type": "set_auth",
                "authType": "bearer",
                "requiresUserInput": True,
                "userInputPrompt": "Enter Bearer Token"
            }
        }

    # 2. Check if a past attempt succeeded on a similar endpoint with a typo (404 ONLY)
    if status_num == 404 and req.previousAttempts:
        for att in req.previousAttempts:
            att_url = att.get("url") or ""
            if str(att.get("status", "")).startswith("2") and is_similar_url_typo(url, att_url):
                return {
                    "fixable": True,
                    "fixType": "url",
                    "title": "Correct URL Typo",
                    "description": f"In past testing, '{att_url}' succeeded with 200 OK.",
                    "confirmationPrompt": f"Update URL to '{att_url}'?",
                    "diff": f"- {url}\n+ {att_url}",
                    "actionPayload": {"type": "set_url", "key": "url", "value": att_url}
                }

    # 3. Check for decimal ID in URL
    dec_match = re.search(r"\/(\d+)\.\d+", url)
    if dec_match:
        fixed_url = re.sub(r"\/(\d+)\.\d+", r"/\1", url)
        return {
            "fixable": True, "fixType": "url", "title": "Correct Resource ID to Integer",
            "description": f"Convert decimal ID in URL to integer ({dec_match.group(1)})",
            "confirmationPrompt": f"Update URL to '{fixed_url}'?",
            "diff": f"- {url}\n+ {fixed_url}",
            "actionPayload": {"type": "set_url", "key": "url", "value": fixed_url}
        }

    # 4. Known common 404 typos
    if status_num == 404:
        for typo, fix in [("commentss", "comments"), ("postss", "posts"), ("todoss", "todos")]:
            if typo in url:
                fixed_url = url.replace(typo, fix)
                return {
                    "fixable": True, "fixType": "url", "title": "Correct URL Typo",
                    "description": f"Fixed trailing typo in endpoint path: {fix}",
                    "confirmationPrompt": f"Update URL to '{fixed_url}'?",
                    "diff": f"- {url}\n+ {fixed_url}",
                    "actionPayload": {"type": "set_url", "key": "url", "value": fixed_url}
                }

    # 5. RAG match if available and status matches
    if retrieved and retrieved[0].get("successfulFixUsed"):
        rf = retrieved[0]["successfulFixUsed"]
        if str(retrieved[0].get("failedStatus")) == str(req.status):
            return {
                "fixable": True,
                "fixType": rf.get("fixType", "header"),
                "title": rf.get("title", "Apply Verified Historical Fix"),
                "description": rf.get("description", "Apply proven fix from RAG memory"),
                "confirmationPrompt": "Apply proven fix from history?",
                "diff": rf.get("diff", "+ Applied from past run"),
                "actionPayload": rf.get("actionPayload", {"type": "add_header", "key": "Authorization", "value": "Bearer <token>"})
            }

    return {
        "fixable": False,
        "fixType": "header",
        "title": "Review Request Parameters",
        "description": "Inspect headers, parameters, and payload configuration.",
        "confirmationPrompt": "Inspect current request configuration?",
        "diff": f"Target: {url}",
        "actionPayload": {"type": "inspect", "key": "url", "value": url}
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

    session_history_str = ""
    candidate_typo_url = None
    if req.previousAttempts:
        lines = []
        for att in req.previousAttempts:
            att_url = att.get("url") or ""
            att_status = att.get("status")
            lines.append(f"- URL: {att_url} | Status: {att_status} | Method: {att.get('method')}")
            # Check for candidate typo ONLY on 404
            if (
                str(req.status) == "404"
                and str(att_status).startswith("2")
                and is_similar_url_typo(req.url, att_url)
            ):
                candidate_typo_url = att_url
        session_history_str = "\nRelevant Past Session Attempts:\n" + "\n".join(lines)

    resp_str = json.dumps(req.response) if isinstance(req.response, (dict, list)) else str(req.response or "")
    if len(resp_str) > 1200:
        resp_str = resp_str[:1200] + "... [truncated]"

    user_prompt = f"""Failed Request Details:
Method: {req.method}
URL: {req.url}
HTTP Status: {req.status}
Duration: {req.duration}ms
Headers: {json.dumps(req.headers or {})}
Body: {json.dumps(req.body) if req.body else 'None'}
Response Body / Stack Trace:
{resp_str}
{session_history_str}
RAG Retrieved Precedents:
{rag_context}"""

    try:
        content = await call_groq_with_fallback(
            [{"role": "system", "content": RAG_FAILURE_PROMPT}, {"role": "user", "content": user_prompt}],
            temperature=0.2, max_tokens=600, is_json=True
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

        # Check for 404 verified typo in history
        if candidate_typo_url and str(req.status) == "404":
            if not diag.get("autoFix") or diag.get("autoFix", {}).get("fixType") != "url":
                diag["autoFix"] = {
                    "fixable": True,
                    "fixType": "url",
                    "title": "Use Proven URL from History",
                    "description": f"In past testing, '{candidate_typo_url}' succeeded with 200 OK.",
                    "confirmationPrompt": f"Update URL to '{candidate_typo_url}'?",
                    "diff": f"- {req.url}\n+ {candidate_typo_url}",
                    "actionPayload": {"type": "set_url", "key": "url", "value": candidate_typo_url}
                }
            if candidate_typo_url not in diag.get("why", ""):
                diag["why"] = f"In past testing, '{candidate_typo_url}' succeeded (200 OK). Current URL has a typo."

        # Guardrails for Auth (401 / Token / Unauthorized) errors:
        # Never allow URL typo fixes for auth failures!
        is_auth_error = (
            str(req.status) == "401"
            or "token" in resp_str.lower()
            or "jwt" in resp_str.lower()
            or "unauthorized" in resp_str.lower()
        )
        if is_auth_error:
            if diag.get("rootCause"):
                diag["rootCause"]["predictedLayer"] = "JWT / Authentication"
            current_fix = diag.get("autoFix")
            if not current_fix or current_fix.get("fixType") == "url" or not current_fix.get("actionPayload"):
                diag["autoFix"] = _build_default_fix(req, retrieved)

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