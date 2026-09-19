"""
MicrodyneHunter v2 — AI Client
Unified AI interface. Priority: OpenRouter -> Gemini -> Anthropic -> None
"""

import logging
import re

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
        return None

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    for model in _openrouter_models():
        try:
            text, status, body = _openrouter_once(api_key, model, messages, max_tokens)
            if text:
                return text

            if status == 402:
                # Retry this model once within what the balance allows.
                afford = _AFFORD.search(body)
                budget = int(afford.group(1)) if afford else 0
                if budget >= 64:
                    logger.warning(f"[AI] {model}: trimming to {budget} tokens to fit the balance")
                    text, status, body = _openrouter_once(api_key, model, messages, budget)
                    if text:
                        return text
                logger.warning(f"[AI] {model}: out of credit, trying the next model")
                continue
            if status in (429, 404, 502, 503):
                logger.warning(f"[AI] {model}: {status}, trying the next model")
                continue

            logger.error(f"[AI] OpenRouter error {status} on {model}: {body}")
        except Exception as e:
            logger.warning(f"[AI] {model} failed ({e}), trying the next model")
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
            return text
        except Exception as e:
            logger.error(f"[AI] Gemini error: {e}")

    # 3. Fallback to Anthropic
    anthropic_client = _get_anthropic()
    if anthropic_client:
        try:
            messages = [{"role": "user", "content": prompt}]
            kwargs = {"model": "claude-haiku-4-5-20251001", "max_tokens": max_tokens, "messages": messages}
            if system:
                kwargs["system"] = system
            response = anthropic_client.messages.create(**kwargs)
            return response.content[0].text.strip()
        except Exception as e:
            logger.error(f"[AI] Anthropic error: {e}")

    return None


def is_ai_available(purpose: str = "") -> bool:
    """Check if any AI provider is configured for this kind of work."""
    anthropic_key = (getattr(config, "ANTHROPIC_API_KEY", "") or "")
    return (
        bool(_openrouter_key(purpose))
        or bool(getattr(config, "GEMINI_API_KEY", ""))
        or (bool(anthropic_key) and "your" not in anthropic_key.lower())
    )
