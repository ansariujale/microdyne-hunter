"""
MicrodyneHunter v2 — AI Client
Unified AI interface. Priority: OpenRouter -> Gemini -> Anthropic -> None
"""

import logging
import re
import time

import config
from config import GEMINI_API_KEY, ANTHROPIC_API_KEY, OPENROUTER_API_KEY

logger = logging.getLogger("microdynehunter.ai")


def _openrouter_key(purpose: str = "") -> str:
    """The OpenRouter key for this kind of work.

    Read from config rather than the import-time copy so a key saved in the
    admin panel takes effect without restarting the server.
    """
    if purpose == "keywords":
        dedicated = (getattr(config, "OPENROUTER_API_KEY_KEYWORDS", "") or "").strip()
        if dedicated:
            return dedicated
    return (getattr(config, "OPENROUTER_API_KEY", "") or "").strip()

_gemini_model = None
_anthropic_client = None

# The last outcome per purpose, so the dashboard can say the assistant is not
# working instead of quietly serving canned replies.
_ai_status: dict = {}


def _note(purpose: str, ok: bool, reason: str = "", detail: str = "", model: str = ""):
    _ai_status[purpose or "general"] = {
        "ok": ok, "reason": reason, "detail": detail[:300], "model": model,
        "at": time.time(),
    }


def get_ai_status(purpose: str = "") -> dict:
    """How the last call for this kind of work went. Empty before the first call."""
    return dict(_ai_status.get(purpose or "general", {}))


def all_ai_status() -> dict:
    return {k: dict(v) for k, v in _ai_status.items()}


# Tried in order. The free model is first so the assistant works on an account
# with no credits; the paid one is better and takes over once credits exist.
# Override with OPENROUTER_MODEL (comma-separated for your own fallback chain).
OPENROUTER_MODELS_DEFAULT = [
    "deepseek/deepseek-v4-flash-0731:free",
    "google/gemini-2.5-flash-lite",
]

# "you can only afford 1115" — OpenRouter reserves the whole max_tokens against
# the balance, so a big ask fails even when the reply would be short.
_AFFORD = re.compile(r"can only afford (\d+)")


def _openrouter_models() -> list[str]:
    configured = (getattr(config, "OPENROUTER_MODEL", "") or "").strip()
    if configured:
        return [m.strip() for m in configured.split(",") if m.strip()]
    return list(OPENROUTER_MODELS_DEFAULT)


def _openrouter_once(api_key: str, model: str, messages: list, max_tokens: int):
    """One request. Returns (text, status_code, body)."""
    import httpx
    resp = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "max_tokens": max(64, min(max_tokens, 1500)),
            "temperature": 0.7,
            "messages": messages,
        },
        timeout=60,
    )
    if resp.status_code == 200:
        try:
            return resp.json()["choices"][0]["message"]["content"].strip(), 200, ""
        except (KeyError, IndexError, ValueError) as e:
            return None, 200, f"unreadable reply: {e}"
    return None, resp.status_code, resp.text[:300]


def _call_openrouter(prompt: str, max_tokens: int, system: str = None,
                     purpose: str = "") -> str | None:
    """Call OpenRouter, working down the model list when one can't serve us.

    A model can fail for reasons the next one won't share: no credit for that
    price tier (402), rate limited (429), or retired (404). Falling through
    keeps the assistant answering instead of dropping to canned replies.
    """
    api_key = _openrouter_key(purpose)
    if not api_key:
        _note(purpose, False, "no_key", "No OpenRouter key is configured.")
        return None

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_status, last_body, last_model = 0, "", ""
    for model in _openrouter_models():
        try:
            text, status, body = _openrouter_once(api_key, model, messages, max_tokens)
            if text:
                _note(purpose, True, model=model)
                return text

            last_status, last_body, last_model = status, body, model
            if status == 402:
                # Retry this model once within what the balance allows.
                afford = _AFFORD.search(body)
                budget = int(afford.group(1)) if afford else 0
                if budget >= 64:
                    logger.warning(f"[AI] {model}: trimming to {budget} tokens to fit the balance")
                    text, status, body = _openrouter_once(api_key, model, messages, budget)
                    if text:
                        _note(purpose, True, model=model)
                        return text
                logger.warning(f"[AI] {model}: out of credit, trying the next model")
                continue
            if status in (429, 404, 502, 503):
                logger.warning(f"[AI] {model}: {status}, trying the next model")
                continue

            logger.error(f"[AI] OpenRouter error {status} on {model}: {body}")
        except Exception as e:
            last_status, last_body, last_model = 0, str(e), model
            logger.warning(f"[AI] {model} failed ({e}), trying the next model")

    reason = {402: "out_of_credit", 429: "rate_limited", 401: "invalid_key",
              403: "invalid_key", 404: "model_unavailable"}.get(last_status, "request_failed")
    _note(purpose, False, reason, last_body or f"HTTP {last_status}", last_model)
    return None


def _get_gemini():
    """Lazy-init Gemini client."""
    global _gemini_model
    if _gemini_model is None and GEMINI_API_KEY:
        try:
            from google import genai
            _gemini_model = genai.Client(api_key=GEMINI_API_KEY)
            logger.info("[AI] Gemini initialized (google-genai)")
        except Exception as e:
            logger.error(f"[AI] Gemini init failed: {e}")
    return _gemini_model


def _get_anthropic():
    """Lazy-init Anthropic client."""
    global _anthropic_client
    if _anthropic_client is None and ANTHROPIC_API_KEY and "your" not in ANTHROPIC_API_KEY.lower():
        try:
            import anthropic
            _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            logger.info("[AI] Anthropic initialized")
        except Exception as e:
            logger.error(f"[AI] Anthropic init failed: {e}")
    return _anthropic_client


def ai_generate(prompt: str, max_tokens: int = 2000, system: str = None,
                purpose: str = "") -> str | None:
    """
    Generate text using available AI provider.
    Priority: OpenRouter -> Gemini -> Anthropic -> None
    `purpose="keywords"` uses the dedicated keyword key when one is configured.
    Returns the text response or None.
    """
    # 1. Try OpenRouter first (currently working)
    result = _call_openrouter(prompt, max_tokens, system, purpose=purpose)
    if result:
        return result

    # 2. Try Gemini
    gemini = _get_gemini()
    if gemini:
        try:
            full_prompt = f"{system}\n\n{prompt}" if system else prompt
            response = gemini.models.generate_content(
                model="gemini-2.0-flash",
                contents=full_prompt,
                config={"max_output_tokens": max_tokens, "temperature": 0.7},
            )
            text = response.text.strip()
            _note(purpose, True, model="gemini-2.0-flash")
            return text
        except Exception as e:
            logger.error(f"[AI] Gemini error: {e}")
            _note(purpose, False, "request_failed", str(e), "gemini-2.0-flash")

    # 3. Fallback to Anthropic
    anthropic_client = _get_anthropic()
    if anthropic_client:
        try:
            messages = [{"role": "user", "content": prompt}]
            kwargs = {"model": "claude-haiku-4-5-20251001", "max_tokens": max_tokens, "messages": messages}
            if system:
                kwargs["system"] = system
            response = anthropic_client.messages.create(**kwargs)
            _note(purpose, True, model=kwargs["model"])
            return response.content[0].text.strip()
        except Exception as e:
            logger.error(f"[AI] Anthropic error: {e}")
            _note(purpose, False, "request_failed", str(e), kwargs.get("model", "anthropic"))

    if not _ai_status.get(purpose or "general", {}).get("at"):
        _note(purpose, False, "no_provider", "No AI provider is configured.")
    return None


def test_openrouter_key(api_key: str) -> dict:
    """Ask OpenRouter about a key without spending anything on a completion."""
    api_key = (api_key or "").strip()
    if not api_key:
        return {"ok": False, "reason": "no_key", "message": "No key given."}
    try:
        import httpx
        resp = httpx.get("https://openrouter.ai/api/v1/key",
                         headers={"Authorization": f"Bearer {api_key}"}, timeout=25)
        if resp.status_code in (401, 403):
            return {"ok": False, "reason": "invalid_key",
                    "message": "OpenRouter rejected this key."}
        if resp.status_code != 200:
            return {"ok": False, "reason": "request_failed",
                    "message": f"OpenRouter returned {resp.status_code}."}
        data = resp.json().get("data", {})
        usage = data.get("usage")
        limit = data.get("limit")
        remaining = None if limit is None else round(limit - (usage or 0), 4)
        free_tier = bool(data.get("is_free_tier"))
        if remaining is not None and remaining <= 0:
            return {"ok": False, "reason": "out_of_credit", "usage": usage, "limit": limit,
                    "remaining": remaining, "free_tier": free_tier,
                    "message": "This key has no credit left."}
        where = "free tier" if free_tier else "paid"
        spend = f"${usage:.4f} used" if usage is not None else "usage unknown"
        left = f", ${remaining} left" if remaining is not None else ""
        return {"ok": True, "reason": "valid", "usage": usage, "limit": limit,
                "remaining": remaining, "free_tier": free_tier,
                "message": f"Key works ({where}) — {spend}{left}."}
    except Exception as e:
        return {"ok": False, "reason": "request_failed", "message": f"Couldn't reach OpenRouter: {e}"}


def is_ai_available(purpose: str = "") -> bool:
    """Check if any AI provider is configured for this kind of work."""
    anthropic_key = (getattr(config, "ANTHROPIC_API_KEY", "") or "")
    return (
        bool(_openrouter_key(purpose))
        or bool(getattr(config, "GEMINI_API_KEY", ""))
        or (bool(anthropic_key) and "your" not in anthropic_key.lower())
    )
