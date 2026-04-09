"""
FlowLockHunter v2 — Email Warmup Manager
Tracks daily send volume per domain with ramp-up schedule.
Persists warmup state in Supabase email_warmup table.
"""

import logging
from datetime import datetime, timezone, date

from config import WARMUP_SCHEDULE, EMAILS_PER_DOMAIN, SENDING_DOMAINS

logger = logging.getLogger("flowlockhunter.warmup")


def _get_db():
    """Lazy import to avoid circular imports."""
    from modules.database import db
    return db


def get_warmup_day(domain: str) -> int:
    """Get the current warmup day for a domain (1-based)."""
    db = _get_db()
    if not db:
        return 1
    today = date.today().isoformat()
    rows = db.select("email_warmup", columns="warmup_day",
                     filters={"domain": f"eq.{domain}", "send_date": f"eq.{today}"},
                     limit=1)
    if rows:
        return rows[0].get("warmup_day", 1)
    # Check yesterday's warmup day to continue progression
    from datetime import timedelta
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    prev = db.select("email_warmup", columns="warmup_day,emails_sent",
                     filters={"domain": f"eq.{domain}", "send_date": f"eq.{yesterday}"},
                     limit=1)
    if prev and prev[0].get("emails_sent", 0) > 0:
        return min(prev[0]["warmup_day"] + 1, max(WARMUP_SCHEDULE.keys()) + 1)
    elif prev:
        return prev[0].get("warmup_day", 1)  # don't advance if no emails sent
    return 1  # brand new domain


def get_daily_limit(domain: str) -> int:
    """Get today's max emails for a domain based on warmup progress."""
    day = get_warmup_day(domain)
    # Find the limit for this day from the schedule
    limit = EMAILS_PER_DOMAIN  # default to steady state
    for schedule_day in sorted(WARMUP_SCHEDULE.keys(), reverse=True):
        if day >= schedule_day:
            limit = WARMUP_SCHEDULE[schedule_day]
            break
    else:
        limit = WARMUP_SCHEDULE.get(1, 5)
    return limit


def get_emails_sent_today(domain: str) -> int:
    """Get how many emails have been sent from this domain today."""
    db = _get_db()
    if not db:
        return 0
    today = date.today().isoformat()
    rows = db.select("email_warmup", columns="emails_sent",
                     filters={"domain": f"eq.{domain}", "send_date": f"eq.{today}"},
                     limit=1)
    if rows:
        return rows[0].get("emails_sent", 0)
    return 0


def get_remaining_capacity(domain: str) -> int:
    """Get how many more emails can be sent from this domain today."""
    limit = get_daily_limit(domain)
    sent = get_emails_sent_today(domain)
    return max(0, limit - sent)


def record_send(domain: str) -> bool:
    """
    Record that an email was sent from this domain.
    Returns True if send is within limits, False if over capacity.
    """
    db = _get_db()
    if not db:
        return True  # in-memory mode, allow all

    today = date.today().isoformat()
    warmup_day = get_warmup_day(domain)
    limit = get_daily_limit(domain)

    existing = db.select("email_warmup",
                         filters={"domain": f"eq.{domain}", "send_date": f"eq.{today}"},
                         limit=1)

    if existing:
        entry = existing[0]
        sent = entry.get("emails_sent", 0)
        if sent >= limit:
            logger.warning(f"[Warmup] {domain} at capacity ({sent}/{limit}) — blocking send")
            return False
        db.update("email_warmup",
                  {"emails_sent": sent + 1},
                  {"id": f"eq.{entry['id']}"})
        logger.debug(f"[Warmup] {domain}: {sent + 1}/{limit} (day {warmup_day})")
    else:
        db.insert("email_warmup", {
            "domain": domain,
            "send_date": today,
            "emails_sent": 1,
            "daily_limit": limit,
            "warmup_day": warmup_day,
        })
        logger.info(f"[Warmup] {domain}: 1/{limit} (day {warmup_day} — new entry)")

    return True


def get_best_domain() -> str | None:
    """
    Pick the sending domain with the most remaining capacity today.
    Returns None if all domains are at capacity.
    Skips any domain that has exceeded or reached its daily limit.
    """
    best_domain = None
    best_remaining = 0

    for domain in SENDING_DOMAINS:
        limit = get_daily_limit(domain)
        sent = get_emails_sent_today(domain)
        # Hard block: skip any domain at or over limit
        if sent >= limit:
            logger.debug(f"[Warmup] {domain}: BLOCKED ({sent}/{limit})")
            continue
        remaining = limit - sent
        if remaining > best_remaining:
            best_remaining = remaining
            best_domain = domain

    if best_domain:
        logger.debug(f"[Warmup] Best domain: {best_domain} ({best_remaining} remaining)")
    else:
        logger.warning("[Warmup] All domains at capacity — no sends possible today")

    return best_domain


def get_total_remaining_capacity() -> int:
    """Get total remaining capacity across all domains."""
    return sum(get_remaining_capacity(d) for d in SENDING_DOMAINS)


def get_warmup_status() -> list[dict]:
    """Get warmup status for all domains (for dashboard)."""
    status = []
    for domain in SENDING_DOMAINS:
        day = get_warmup_day(domain)
        limit = get_daily_limit(domain)
        sent = get_emails_sent_today(domain)
        exceeded = sent > limit
        at_capacity = sent >= limit
        usage_pct = round(sent / limit * 100, 1) if limit > 0 else 0

        # Health label based on capacity usage
        if exceeded:
            health = "exceeded"
        elif at_capacity:
            health = "limit"
        elif usage_pct >= 80:
            health = "poor"
        elif usage_pct >= 50:
            health = "moderate"
        elif sent == 0:
            health = "new"
        else:
            health = "healthy"

        status.append({
            "domain": domain,
            "warmup_day": day,
            "daily_limit": limit,
            "emails_sent": sent,
            "remaining": max(0, limit - sent),
            "at_capacity": at_capacity,
            "exceeded": exceeded,
            "usage_percent": usage_pct,
            "health": health,
        })
    return status
