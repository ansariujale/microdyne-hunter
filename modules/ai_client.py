"""
FlowLockHunter v2 — AI Client
Unified AI interface. Priority: OpenRouter -> Gemini -> Anthropic -> None
"""

import logging

from config import GEMINI_API_KEY, ANTHROPIC_API_KEY, OPENROUTER_API_KEY

logger = logging.getLogger("flowlockhunter.ai")

_gemini_model = None
_anthropic_client = None


def _call_openrouter(prompt: str, max_tokens: int, system: str = None) -> str | None:
    """Call OpenRouter API (OpenAI-compatible endpoint)."""
    if not OPENROUTER_API_KEY:
        return None
    try:
        import httpx
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "google/gemini-2.0-flash-001",
                "max_tokens": min(max_tokens, 1500),
                "temperature": 0.7,
                "messages": messages,
            },
            timeout=60,
        )
        if resp.status_code == 200:
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            return text
        else:
            logger.error(f"[AI] OpenRouter error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"[AI] OpenRouter error: {e}")
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


def ai_generate(prompt: str, max_tokens: int = 2000, system: str = None) -> str | None:
    """
    Generate text using available AI provider.
    Priority: OpenRouter -> Gemini -> Anthropic -> None
    Returns the text response or None.
    """
    # 1. Try OpenRouter first (currently working)
    result = _call_openrouter(prompt, max_tokens, system)
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


def is_ai_available() -> bool:
    """Check if any AI provider is configured."""
    return (
        bool(OPENROUTER_API_KEY)
        or bool(GEMINI_API_KEY)
        or (bool(ANTHROPIC_API_KEY) and "your" not in ANTHROPIC_API_KEY.lower())
    )
