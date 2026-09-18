"""
MicrodyneHunter v2 — AI keyword generation

Suggests new lead-generation search keywords from the company profile and what
has already been searched, then drops anything that duplicates or closely
resembles an existing keyword before the admin ever sees it.
"""

import json
import logging
import re
from difflib import SequenceMatcher

import config
from modules.database import db

logger = logging.getLogger("microdynehunter.keywords")

# Above this similarity two keywords search for the same companies, so the
# newer one is not worth spending a scrape on.
SIMILARITY_LIMIT = 0.82

# Words that carry no distinguishing meaning when comparing two keywords.
_FILLER = {"the", "a", "an", "of", "for", "and", "in", "&", "co", "company", "companies"}


def _normalize(keyword: str) -> str:
    text = str(keyword or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


def _tokens(keyword: str) -> set[str]:
    words = []
    for word in _normalize(keyword).split():
        if word in _FILLER:
            continue
        words.append(word[:-1] if len(word) > 4 and word.endswith("s") else word)
    return set(words)


def similarity(a: str, b: str) -> float:
    """1.0 for the same keyword, high for near-duplicates like plural or word-order variants."""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = _tokens(a), _tokens(b)
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return max(jaccard, SequenceMatcher(None, na, nb).ratio())


def is_too_similar(keyword: str, existing: list[str]) -> tuple[bool, str, float]:
    """The closest existing keyword, and whether it is close enough to skip the new one."""
    best, score = "", 0.0
    for other in existing:
        value = similarity(keyword, other)
        if value > score:
            best, score = other, value
    return score >= SIMILARITY_LIMIT, best, round(score, 3)


def get_used_keywords(limit: int = 4000) -> list[str]:
    """Every keyword already configured or already searched."""
    used = list(getattr(config, "SEARCH_KEYWORDS", []) or [])
    if not db:
        return used

    seen = {_normalize(k) for k in used}

    def collect(rows, field):
        for row in rows or []:
            raw = row.get(field) or ""
            # Search rows are stored as "<keyword> <country>"; keep the whole
            # phrase, the country suffix barely moves the similarity score.
            for part in str(raw).split(","):
                part = " ".join(part.split())
                if part and _normalize(part) not in seen:
                    seen.add(_normalize(part))
                    used.append(part)

    try:
        collect(db.select("source_tracker", columns="keyword", limit=limit), "keyword")
    except Exception as e:
        logger.debug(f"[Keywords] source_tracker unavailable: {e}")
    try:
        collect(db.select("leads", columns="keyword_used", limit=limit), "keyword_used")
    except Exception as e:
        logger.debug(f"[Keywords] leads.keyword_used unavailable: {e}")
    return used


_PROMPT = """You are building the search-keyword list for {company}'s B2B \
lead-generation agent.

ABOUT THE COMPANY
- Name: {company}
- Based in: {location}
- What it sells: {products}
- Materials: {materials}
- Positioning: {usp}

HOW THE KEYWORDS ARE USED
Each keyword is searched on Google Maps and web search with a country appended, \
for example "pump manufacturer Germany". The results become leads who are then \
emailed an offer of CNC turning job work.

WHO WE WANT TO FIND
Manufacturers that CONSUME precision turned components — screws, nuts, sleeves, \
bushings, shafts, seal parts — and that outsource turning work. These are the \
buyers. Current buyer categories: {lead_types}

WHO WE MUST NOT FIND
Other CNC job shops, turning shops, machining subcontractors or lathe workshops. \
They are competitors, not customers. Never suggest a keyword that would return them \
(nothing containing "CNC job work", "turning services", "machining subcontractor" \
or similar).

ALREADY IN USE — do not repeat these or produce close variants of them:
{used}

Suggest {count} NEW keywords that are meaningfully different from everything above. \
Each should name a type of manufacturer or industrial equipment producer, in the form \
a buyer's website or Google Maps listing would actually use. No country names, no \
punctuation, lower case, two to five words each.

Reply with JSON only, no other text:
{{"keywords": [{{"keyword": "<search phrase>", "why": "<why they buy turned parts, max 12 words>"}}]}}"""


def generate_keywords(count: int = 12) -> dict:
    """
    Suggest new keywords for the admin to review.

    Returns the accepted suggestions plus everything that was rejected and why,
    so the panel can show that duplicates were filtered rather than silently lost.
    """
    from modules.ai_client import ai_generate, is_ai_available

    used = get_used_keywords()
    result = {
        "keywords": [], "rejected": [], "used_count": len(used),
        "source": "none", "error": "",
    }

    if not is_ai_available("keywords"):
        result["error"] = ("No AI provider is configured. Add an OpenRouter, Gemini or "
                           "Anthropic key under API Keys first.")
        return result

    prompt = _PROMPT.format(
        company=config.MICRODYNE["company_name"],
        location=config.MICRODYNE["address"],
        products="; ".join(config.MICRODYNE["products"]),
        materials=config.MICRODYNE["materials"],
        usp=config.MICRODYNE["usp"],
        lead_types=", ".join(t.replace("_", " ") for t in getattr(config, "LEAD_TYPES", [])),
        used="\n".join(f"- {k}" for k in used[:120]) or "- nothing yet",
        # Ask for extra so there is still a useful list after near-duplicates go.
        count=min(30, max(count + 6, 12)),
    )

    try:
        raw = ai_generate(prompt, max_tokens=1200, purpose="keywords")
    except Exception as e:
        logger.error(f"[Keywords] Generation failed: {e}")
        result["error"] = f"The AI request failed: {e}"
        return result

    suggestions = _parse_keyword_reply(raw)
    if not suggestions:
        result["error"] = "The AI reply couldn't be read as a keyword list. Try again."
        return result

    result["source"] = "ai"
    accepted: list[dict] = []
    for item in suggestions:
        keyword = config.normalize_keyword(item.get("keyword", "")).lower()
        if not keyword or len(keyword) < 4:
            continue
        pool = used + [a["keyword"] for a in accepted]
        too_close, closest, score = is_too_similar(keyword, pool)
        if too_close:
            result["rejected"].append({
                "keyword": keyword, "reason": "too similar to a keyword already in use",
                "closest": closest, "similarity": score,
            })
            continue
        accepted.append({
            "keyword": keyword,
            "why": " ".join(str(item.get("why", "")).split())[:120],
            "closest_existing": closest,
            "similarity": score,
        })
        if len(accepted) >= count:
            break

    result["keywords"] = accepted
    if not accepted:
        result["error"] = ("Every suggestion was already covered by an existing keyword. "
                           "The current list looks saturated.")
    return result


def _parse_keyword_reply(raw: str | None) -> list[dict]:
    """Read the JSON payload; fall back to bullet lines if the model wrapped it in prose."""
    if not raw:
        return []
    text = str(raw).strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            items = data.get("keywords") or []
            if isinstance(items, list):
                out = []
                for item in items:
                    if isinstance(item, str):
                        out.append({"keyword": item, "why": ""})
                    elif isinstance(item, dict) and item.get("keyword"):
                        out.append(item)
                if out:
                    return out
        except Exception:
            pass

    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip().strip('",')
        if 4 <= len(line) <= 60 and not line.startswith("{") and ":" not in line:
            out.append({"keyword": line, "why": ""})
    return out
