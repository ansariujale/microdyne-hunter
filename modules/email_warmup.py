"""
FlowLockHunter v2 — Email Warmup Manager
Tracks daily send volume per domain with ramp-up schedule.
Persists warmup state in Supabase email_warmup table.
"""

import logging
from datetime import datetime, timezone, date, timedelta

from config import EMAILS_PER_DOMAIN, SENDING_EMAILS

logger = logging.getLogger("flowlockhunter.warmup")

def _send_date(reset_hour_local: int = 11) -> date:
    now_local = datetime.now(timezone.utc).astimezone()
    d = now_local.date()
    if now_local.hour < reset_hour_local:
        d = d - timedelta(days=1)
    return d


def _get_db():
    """Lazy import to avoid circular imports."""
    from modules.database import db
    return db


def get_warmup_day(domain: str) -> int:
    """Get the current warmup day for a domain (1-based)."""
    db = _get_db()
    if not db:
        return 1
    today = _send_date(reset_hour_local=11).isoformat()
    rows = db.select("email_warmup", columns="warmup_day",
                     filters={"domain": f"eq.{domain}", "send_date": f"eq.{today}"},
                     limit=1)
    if rows:
        return rows[0].get("warmup_day", 1)
    # Check yesterday's warmup day to continue progression
    yesterday = (_send_date(reset_hour_local=11) - timedelta(days=1)).isoformat()
    prev = db.select("email_warmup", columns="warmup_day,emails_sent",
                     filters={"domain": f"eq.{domain}", "send_date": f"eq.{yesterday}"},
                     limit=1)
    if prev and prev[0].get("emails_sent", 0) > 0:
        return min(prev[0]["warmup_day"] + 1, max(WARMUP_SCHEDULE.keys()) + 1)
    elif prev:
        return prev[0].get("warmup_day", 1)  # don't advance if no emails sent
    return 1  # brand new domain


def get_daily_limit(domain: str) -> int:
    return EMAILS_PER_DOMAIN


def get_emails_sent_today(domain: str) -> int:
    """Get how many emails have been sent from this domain today."""
    db = _get_db()
    if not db:
        return 0
    today = _send_date(reset_hour_local=11).isoformat()
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
    Record that an email attempt was made by this sender.
    Returns True if within limits, False if at/over capacity.
    """
    db = _get_db()
    if not db:
        return True  # in-memory mode, allow all

    today = _send_date(reset_hour_local=11).isoformat()
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

    for domain in SENDING_EMAILS:
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
    return sum(get_remaining_capacity(d) for d in SENDING_EMAILS)


def get_warmup_status() -> list[dict]:
    """Get warmup status for all domains (for dashboard)."""
    status = []
    for domain in SENDING_EMAILS:
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
