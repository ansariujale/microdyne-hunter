"""
MicrodyneHunter v2 — Country rotation

Picks the market for the next lead-generation campaign. The order is fixed:
start from the configured pool, drop Gulf countries, drop anything still in
cooldown, then choose from what is left. The AI only ever chooses among
already-eligible countries — it never widens the list.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import config
from modules.database import db

logger = logging.getLogger("microdynehunter.country")


# ═══════════════════════════════════════════════════════════════
# HISTORY
# ═══════════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp) -> datetime | None:
    if not stamp:
        return None
    try:
        text = str(stamp).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def get_country_history() -> list[dict]:
    """Country | Last Used | Usage Count | Next Eligible Date, newest use first."""
    if not db:
        return []
    try:
        rows = db.select("country_usage", order="last_used_at.desc", limit=500) or []
    except Exception as e:
        logger.warning(f"[Country] History unavailable: {e}")
        return []

    now = _now()
    history = []
    for row in rows:
        next_at = _parse(row.get("next_eligible_at"))
        history.append({
            "country": row.get("country"),
            "last_used_at": row.get("last_used_at"),
            "usage_count": int(row.get("usage_count") or 0),
            "next_eligible_at": row.get("next_eligible_at"),
            "eligible_now": next_at is None or next_at <= now,
            "days_until_eligible": max(0, (next_at - now).days) if next_at and next_at > now else 0,
            "selected_by": row.get("selected_by") or "manual",
            "last_reason": row.get("last_reason") or "",
        })
    return history


def record_country_use(country: str, selected_by: str = "ai", reason: str = "",
                       cooldown_days: int | None = None) -> dict | None:
    """Stamp a country as used now and start its cooldown."""
    country = (country or "").strip()
    if not country or not db:
        return None
    cooldown = int(cooldown_days if cooldown_days is not None
                   else getattr(config, "COUNTRY_COOLDOWN_DAYS", 14))
    now = _now()
    payload = {
        "country": country,
        "last_used_at": now.isoformat().replace("+00:00", "Z"),
        "next_eligible_at": (now + timedelta(days=cooldown)).isoformat().replace("+00:00", "Z"),
        "cooldown_days": cooldown,
        "selected_by": selected_by,
        "last_reason": (reason or "")[:500],
        "updated_at": now.isoformat().replace("+00:00", "Z"),
    }
    try:
        existing = db.select("country_usage", filters={"country": f"eq.{country}"}, limit=1)
        if existing:
            payload["usage_count"] = int(existing[0].get("usage_count") or 0) + 1
            db.update("country_usage", payload, {"id": f"eq.{existing[0]['id']}"})
        else:
            payload["usage_count"] = 1
            db.insert("country_usage", payload)
        logger.info(f"[Country] {country} recorded as used — next eligible in {cooldown} days")
        return payload
    except Exception as e:
        logger.error(f"[Country] Couldn't record {country}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# ELIGIBILITY
# ═══════════════════════════════════════════════════════════════

def _canonical(name: str) -> str:
    return " ".join(str(name or "").strip().lower().split())


def get_eligible_countries(pool: list[str] | None = None) -> dict:
    """Split the pool into eligible / excluded, and say why each one was dropped."""
    candidates = list(pool or getattr(config, "COUNTRY_POOL", []) or [])
    history = {_canonical(h["country"]): h for h in get_country_history()}
    now = _now()

    eligible, gulf_blocked, cooling = [], [], []
    for country in candidates:
        if config.is_gulf_country(country):
            gulf_blocked.append(country)          # never selectable, whatever the history
            continue
        record = history.get(_canonical(country))
        next_at = _parse(record.get("next_eligible_at")) if record else None
        if next_at and next_at > now:
            cooling.append({
                "country": country,
                "next_eligible_at": record.get("next_eligible_at"),
                "days_left": max(1, (next_at - now).days + (1 if (next_at - now).seconds else 0)),
                "usage_count": record.get("usage_count", 0),
            })
            continue
        eligible.append({
            "country": country,
            "last_used_at": record.get("last_used_at") if record else None,
            "usage_count": record.get("usage_count", 0) if record else 0,
        })

    # Never used first, then longest since last use, then least used overall.
    eligible.sort(key=lambda c: (c["last_used_at"] or "", c["usage_count"]))
    return {
        "eligible": eligible,
        "gulf_excluded": gulf_blocked,
        "cooling_down": sorted(cooling, key=lambda c: c["days_left"]),
        "cooldown_days": int(getattr(config, "COUNTRY_COOLDOWN_DAYS", 14)),
    }


def _country_performance(limit: int = 5000) -> dict:
    """Leads and replies per country so far, to inform the choice."""
    if not db:
        return {}
    try:
        rows = db.select("leads", columns="country,replied,closed", limit=limit) or []
    except Exception:
        return {}
    stats: dict[str, dict] = {}
    for row in rows:
        name = (row.get("country") or "").strip()
        if not name:
            continue
        entry = stats.setdefault(name, {"leads": 0, "replies": 0, "closes": 0})
        entry["leads"] += 1
        if row.get("replied"):
            entry["replies"] += 1
        if row.get("closed"):
            entry["closes"] += 1
    return stats


# ═══════════════════════════════════════════════════════════════
# SELECTION
# ═══════════════════════════════════════════════════════════════

_PROMPT = """You are choosing the target market for the next B2B lead-generation \
campaign run by {company}.

{company} is a CNC turning job-work manufacturer in {location}. It does turning \
work only — no milling. It sells subcontract turning capacity and precision turned \
components to manufacturers that consume such parts: {products}

Pick ONE country for the next campaign from the eligible list below. You may not \
pick anything outside the list — every other country is either on cooldown or \
excluded, and a choice outside the list will be rejected.

ELIGIBLE COUNTRIES (already filtered — pick exactly one of these):
{eligible}

RECENT CAMPAIGN HISTORY (most recent first, these are on cooldown):
{history}

RESULTS SO FAR (leads found / replies received per country):
{performance}

Choose on merit, not at random. Favour a country with a dense base of pump, valve, \
hydraulic, compressor, gearbox and machinery manufacturers that outsource turned \
components, where business is conducted in English or where English enquiries are \
normal, and where this company has not campaigned recently.

Reply with JSON only, no other text:
{{"country": "<one country from the eligible list>", "reason": "<one sentence, max 30 words>", \
"runner_up": "<second choice from the list>"}}"""


def _fallback_choice(eligible: list[dict]) -> tuple[str, str]:
    """Least recently used, then least used overall. Deterministic, never random."""
    if not eligible:
        return "", ""
    first = eligible[0]
    if not first.get("last_used_at"):
        return first["country"], "Never campaigned in before, so it is the freshest market in the pool."
    return first["country"], "Longest time since the last campaign there."


def select_next_country(use_ai: bool = True) -> dict:
    """
    Pick the next campaign country.

    Order is always: pool → drop Gulf → drop cooling down → choose. The AI is
    asked to choose among what survives; its answer is checked against that same
    list, and anything else falls back to the least-recently-used country.
    """
    buckets = get_eligible_countries()
    eligible = buckets["eligible"]
    result = {
        "selected": "",
        "reason": "",
        "runner_up": "",
        "source": "none",
        "eligible": eligible,
        "gulf_excluded": buckets["gulf_excluded"],
        "cooling_down": buckets["cooling_down"],
        "cooldown_days": buckets["cooldown_days"],
        "warning": "",
    }

    if not eligible:
        soonest = buckets["cooling_down"][0] if buckets["cooling_down"] else None
        result["warning"] = (
            f"Every country in the pool is still cooling down. "
            f"{soonest['country']} is next, in {soonest['days_left']} day(s)."
            if soonest else "No countries are configured in the pool."
        )
        return result

    fallback, fallback_reason = _fallback_choice(eligible)

    if use_ai:
        try:
            from modules.ai_client import ai_generate, is_ai_available
        except Exception:
            ai_generate, is_ai_available = None, lambda: False
        if is_ai_available():
            history = get_country_history()
            perf = _country_performance()
            prompt = _PROMPT.format(
                company=config.MICRODYNE["company_name"],
                location=config.MICRODYNE["address"],
                products="; ".join(config.MICRODYNE["products"]),
                eligible="\n".join(
                    f"- {c['country']} (used {c['usage_count']}x, "
                    f"last {c['last_used_at'][:10] if c['last_used_at'] else 'never'})"
                    for c in eligible),
                history="\n".join(
                    f"- {h['country']}: last used {str(h['last_used_at'])[:10]}, "
                    f"{h['usage_count']}x total, eligible again {str(h['next_eligible_at'])[:10]}"
                    for h in history[:15]) or "- no campaigns recorded yet",
                performance="\n".join(
                    f"- {name}: {s['leads']} leads, {s['replies']} replies"
                    for name, s in sorted(perf.items(), key=lambda kv: -kv[1]["leads"])[:12]
                ) or "- no results recorded yet",
            )
            try:
                raw = ai_generate(prompt, max_tokens=300)
                pick = _parse_country_reply(raw, [c["country"] for c in eligible])
                if pick:
                    result.update(pick)
                    result["source"] = "ai"
                    return result
                if raw:
                    logger.warning(f"[Country] AI reply unusable, falling back: {str(raw)[:160]}")
                    result["warning"] = "The AI suggested a country outside the eligible list, so the least recently used one was chosen instead."
            except Exception as e:
                logger.error(f"[Country] AI selection failed: {e}")
                result["warning"] = f"AI unavailable ({e}); chose the least recently used eligible country."
        else:
            result["warning"] = "No AI provider configured; chose the least recently used eligible country."

    result["selected"] = fallback
    result["reason"] = fallback_reason
    result["runner_up"] = eligible[1]["country"] if len(eligible) > 1 else ""
    result["source"] = "history"
    return result


def _parse_country_reply(raw: str | None, allowed: list[str]) -> dict | None:
    """Accept the AI's pick only if it names a country from the eligible list."""
    if not raw:
        return None
    text = str(raw).strip()
    match = re.search(r"\{.*\}", text, re.S)
    data = {}
    if match:
        try:
            data = json.loads(match.group(0))
        except Exception:
            data = {}

    by_name = {_canonical(c): c for c in allowed}
    picked = by_name.get(_canonical(data.get("country", "")))
    if not picked:
        # A bare country name, or JSON we couldn't parse — look for any eligible
        # name in the text, longest first so "United States" wins over "States".
        lowered = text.lower()
        for name in sorted(allowed, key=len, reverse=True):
            if _canonical(name) in lowered:
                picked = name
                break
    if not picked or config.is_gulf_country(picked):
        return None

    runner = by_name.get(_canonical(data.get("runner_up", "")), "")
    if runner and config.is_gulf_country(runner):
        runner = ""
    return {
        "selected": picked,
        "reason": " ".join(str(data.get("reason", "")).split())[:300],
        "runner_up": runner,
    }
