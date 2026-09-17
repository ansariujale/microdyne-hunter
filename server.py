#!/usr/bin/env python3
"""
MicrodyneHunter v2 — API Server
Serves the dashboard and exposes API endpoints to control the agent.
Launch with: python server.py
Dashboard opens at: http://localhost:8000
"""

import os
import sys
import json
import time
import base64
import random
import threading
import logging
import webbrowser
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

from rich.console import Console
from rich.logging import RichHandler

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)],
)
logger = logging.getLogger("microdynehunter.server")
console = Console()

# ═══════════════════════════════════════════════════════════════
# ADMIN AUTH
# ═══════════════════════════════════════════════════════════════
import hashlib, secrets

# Fixed fallback credentials — always exist as a recovery login (independent of
# whatever custom username/password is saved below), gated by ALLOW_DEFAULT_LOGIN.
DEFAULT_ADMIN_CREDENTIALS = {
    "username": "@Microdyne",
    "password": "@Microdyne31",
}

ADMIN_CREDENTIALS = {
    "username": os.getenv("ADMIN_USERNAME", DEFAULT_ADMIN_CREDENTIALS["username"]),
    "password": os.getenv("ADMIN_PASSWORD", DEFAULT_ADMIN_CREDENTIALS["password"]),
}

# When True, the fixed DEFAULT_ADMIN_CREDENTIALS above always work as a backup
# login even after the username/password are changed. Toggle off from the
# admin panel to disable that fallback and require the custom credentials only.
ALLOW_DEFAULT_LOGIN = os.getenv("ALLOW_DEFAULT_LOGIN", "true").strip().lower() != "false"

_admin_tokens = set()  # active session tokens

def _check_admin_token(handler) -> bool:
    """Verify admin token from request header."""
    token = handler.headers.get("X-Admin-Token", "")
    return token in _admin_tokens

# ═══════════════════════════════════════════════════════════════
# AGENT STATE
# ═══════════════════════════════════════════════════════════════
agent_state = {
    "status": "idle",           # idle, running, stopped
    "current_step": None,       # which step is running
    "pipeline_running": False,
    "agent_loop_running": False, # continuous loop mode
    "cycle": 0,                 # current cycle number
    "loop_interval": 3600,      # seconds between cycles (1 hour)
    "last_run": None,
    "started_at": None,
    "log": [],                  # recent log entries
    "chat_history": [],         # conversation history for AI chat
    "stats": {
        "leads_scraped": 0,
        "leads_qualified": 0,
        "leads_stored": 0,
        "emails_sent": 0,
        "forms_filled": 0,
        "followups_sent": 0,
    },
    "errors": [],
    "last_business_date": None,
    "daily_reset_notice": None,
    "initial_sync_done": False,
    "lead_target_override": None,
    "lead_target_override_business_date": None,
    "batch_state": None,  # cached {active,batch_id,start_utc_iso,lead_target}
}

MAX_LOG = 200  # keep last N log entries

def add_log(msg, level="info", category="system", data=None):
    """Add a structured log entry to the agent state."""
    entry = {
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "msg": msg,
        "level": level,
        "category": category,
        "data": data,
    }
    agent_state["log"].insert(0, entry)
    if len(agent_state["log"]) > MAX_LOG:
        agent_state["log"] = agent_state["log"][:MAX_LOG]
    logger.info(msg)


def _current_business_date_str(reset_hour_local: int = 11) -> str:
    """Return business-date key in local timezone (resets at 11:00 local)."""
    now_local = datetime.now(timezone.utc).astimezone()
    day = now_local.date()
    if now_local.hour < reset_hour_local:
        day = day - timedelta(days=1)
    return day.isoformat()


def _capture_previous_business_day_snapshot() -> dict:
    """Capture yesterday metrics before resetting in-memory dashboard counters."""
    try:
        from modules.database import db, get_business_day_range
        if not db:
            return {}
        prev_day = datetime.strptime(_current_business_date_str(), "%Y-%m-%d").date() - timedelta(days=1)
        # Recreate the same 11 AM business window for previous day.
        now_local = datetime.now(timezone.utc).astimezone()
        start_local = datetime(prev_day.year, prev_day.month, prev_day.day, 11, 0, 0, tzinfo=now_local.tzinfo)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc).isoformat()
        end_utc = end_local.astimezone(timezone.utc).isoformat()
        return {
            "business_date": prev_day.isoformat(),
            "leads": int(db.count("leads", {"and": f"(created_at.gte.{start_utc},created_at.lt.{end_utc})"}) or 0),
            "emails": int(db.count("outreach_log", {"and": f"(channel.eq.email,sent_at.gte.{start_utc},sent_at.lt.{end_utc})"}) or 0),
            "forms": int(db.count("leads", {"and": f"(form_last_attempted_at.gte.{start_utc},form_last_attempted_at.lt.{end_utc},form_submission_status.neq.pending)"}) or 0),
            "opened": int(db.count("leads", {"and": f"(email_opened.eq.true,email_sent_at.gte.{start_utc},email_sent_at.lt.{end_utc})"}) or 0),
            "replied": int(db.count("leads", {"and": f"(replied.eq.true,replied_at.gte.{start_utc},replied_at.lt.{end_utc})"}) or 0),
            "visible_until": time.time() + 60,
        }
    except Exception as e:
        logger.warning(f"Daily reset snapshot failed: {e}")
        return {}


def _get_batch_state_from_db() -> dict:
    """Load last batch_state from intelligence_reports."""
    try:
        from modules.database import db
        if not db:
            return {}
        rows = db.select(
            "intelligence_reports",
            columns="report_data,created_at",
            filters={"report_type": "eq.batch_state"},
            order="created_at.desc",
            limit=1,
        ) or []
        if not rows:
            return {}
        data = rows[0].get("report_data") or {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_batch_state_to_db(state: dict) -> None:
    """Persist batch_state snapshot into intelligence_reports."""
    try:
        from modules.database import db
        if not db:
            return
        db.insert("intelligence_reports", {"report_type": "batch_state", "report_data": state})
    except Exception:
        return


def _ensure_batch_state_loaded():
    if agent_state.get("batch_state") is not None:
        return
    agent_state["batch_state"] = _get_batch_state_from_db() or {}


def _set_batch_active(start_utc_iso: str, lead_target: int):
    import uuid
    state = {
        "active": True,
        "batch_id": str(uuid.uuid4()),
        "start_utc_iso": start_utc_iso,
        "lead_target": int(lead_target or 120),
        "set_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    agent_state["batch_state"] = state
    _save_batch_state_to_db(state)


def _set_batch_inactive():
    state = {
        "active": False,
        "batch_id": None,
        "start_utc_iso": None,
        "lead_target": 0,
        "set_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    agent_state["batch_state"] = state
    _save_batch_state_to_db(state)


def _daily_business_reset_if_needed():
    """Reset only in-memory run stats at the 11 AM business-day boundary."""
    business_date = _current_business_date_str()
    if agent_state.get("last_business_date") == business_date:
        return

    snapshot = _capture_previous_business_day_snapshot()
    agent_state["stats"]["leads_scraped"] = 0
    agent_state["stats"]["leads_qualified"] = 0
    agent_state["stats"]["leads_stored"] = 0
    agent_state["stats"]["emails_sent"] = 0
    agent_state["stats"]["forms_filled"] = 0
    agent_state["stats"]["followups_sent"] = 0
    agent_state["last_business_date"] = business_date
    agent_state["daily_reset_notice"] = snapshot or None
    # Clear today's override when business date rolls
    agent_state["lead_target_override"] = None
    agent_state["lead_target_override_business_date"] = None
    if snapshot:
        add_log(
            f"Daily reset @11:00 complete — yesterday: {snapshot.get('leads',0)} leads, "
            f"{snapshot.get('emails',0)} emails, {snapshot.get('forms',0)} forms, "
            f"{snapshot.get('opened',0)} opens, {snapshot.get('replied',0)} replies",
            category="system",
            data={"type": "daily_reset", **snapshot},
        )


def _sync_existing_lead_records():
    """
    One-time reconciliation:
    - ensure emailed leads have outreach_log rows
    - ensure form_filled leads are not stuck in pending status
    """
    try:
        from modules.database import db
        if not db:
            return {"synced_email_logs": 0, "fixed_form_status": 0}

        synced_email_logs = 0
        fixed_form_status = 0
        leads = db.select(
            "leads",
            columns="id,email_sent,email_sent_at,sending_domain,form_filled,form_submission_status,form_last_attempted_at",
            limit=5000,
        ) or []

        for lead in leads:
            lead_id = lead.get("id")
            if not lead_id:
                continue

            if lead.get("email_sent"):
                existing = db.count("outreach_log", {"lead_id": f"eq.{lead_id}", "channel": "eq.email"})
                if int(existing or 0) == 0:
                    db.insert("outreach_log", {
                        "lead_id": lead_id,
                        "channel": "email",
                        "sequence_stage": 1,
                        "sending_domain": lead.get("sending_domain") or "unknown",
                        "delivery_status": "sent",
                        "sent_at": lead.get("email_sent_at") or datetime.now(timezone.utc).isoformat(),
                    })
                    synced_email_logs += 1

            if lead.get("form_filled") and (lead.get("form_submission_status") in (None, "", "pending")):
                db.update("leads", {
                    "form_submission_status": "success",
                    "form_last_attempted_at": lead.get("form_last_attempted_at") or datetime.now(timezone.utc).isoformat(),
                }, {"id": f"eq.{lead_id}"})
                fixed_form_status += 1

        if synced_email_logs or fixed_form_status:
            add_log(
                f"Reconciled Supabase data — +{synced_email_logs} email logs, fixed {fixed_form_status} form statuses",
                category="system",
            )
        return {"synced_email_logs": synced_email_logs, "fixed_form_status": fixed_form_status}
    except Exception as e:
        logger.warning(f"Lead reconciliation failed: {e}")
        return {"synced_email_logs": 0, "fixed_form_status": 0}


def _trim_pending_email_queue(keep: int = 40):
    """
    Keep only the newest `keep` unsent leads active for email queue.
    Remaining unsent leads are marked excluded=true (still stored in Supabase).
    """
    try:
        from modules.database import db
        import config
        if not db:
            return {"kept": 0, "excluded": 0}

        min_score = int(getattr(config, "SCORE_THRESHOLDS", {}).get("min_qualify", 40) or 40)
        pending = db.select(
            "leads",
            columns="id,created_at,company_domain,contact_email,email_sent,excluded,score,sequence_stage",
            filters={
                "email_sent": "eq.false",
                "excluded": "eq.false",
                "sequence_stage": "eq.0",
                "score": f"gte.{min_score}",
            },
            order="created_at.desc",
            limit=5000,
        ) or []

        keep = max(0, int(keep or 0))
        if len(pending) <= keep:
            return {"kept": len(pending), "excluded": 0}

        keep_rows = pending[:keep]
        exclude_rows = pending[keep:]

        excluded_count = 0
        for row in exclude_rows:
            lead_id = row.get("id")
            if not lead_id:
                continue
            db.update("leads", {"excluded": True}, {"id": f"eq.{lead_id}"})
            excluded_count += 1

        add_log(
            f"Email queue trimmed — kept {len(keep_rows)} unsent leads, excluded {excluded_count} extra leads",
            category="system",
        )
        return {"kept": len(keep_rows), "excluded": excluded_count}
    except Exception as e:
        logger.warning(f"Queue trim failed: {e}")
        return {"kept": 0, "excluded": 0, "error": str(e)}


def _extra_keywords(raw) -> list[str]:
    """Admin-added keywords only — the preserved best set lives in config and is always merged in."""
    import config
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    best = {k.lower() for k in config.BEST_SEARCH_KEYWORDS}
    extras = []
    for item in items:
        kw = config.normalize_keyword(item)
        if kw and kw.lower() not in best and kw.lower() not in {e.lower() for e in extras}:
            extras.append(kw)
    return extras


# ═══════════════════════════════════════════════════════════════
# PIPELINE RUNNER (runs in background thread)
# ═══════════════════════════════════════════════════════════════

def run_step_thread(step_name):
    """Run a single pipeline step in a background thread."""
    agent_state["status"] = "running"
    agent_state["current_step"] = step_name
    add_log(f"▶ Starting: {step_name}")

    try:
        if step_name == "scrape":
            from modules.scraper import run_daily_scrape
            leads = run_daily_scrape()
            agent_state["stats"]["leads_scraped"] = len(leads)
            agent_state["_temp_leads"] = leads
            add_log(f"✓ Scraped {len(leads)} leads (already inserted into Supabase per-country)")

        elif step_name == "qualify":
            raw = agent_state.get("_temp_leads", [])
            if not raw:
                add_log("⚠ No leads to qualify — run scrape first", "warning")
            else:
                try:
                    from modules.qualifier import qualify_leads
                    qualified = qualify_leads(raw, use_ai=True)
                    agent_state["stats"]["leads_qualified"] = len(qualified)
                    add_log(f"✓ Qualified {len(qualified)} leads")
                except Exception as qe:
                    add_log(f"⚠ Qualifier unavailable ({qe}) — leads already stored", "warning")

        elif step_name == "store":
            from modules.database import get_total_leads
            total = get_total_leads()
            add_log(f"✓ DB has {total} total leads")

        elif step_name == "email":
            from modules.database import get_leads_for_email
            from modules.emailer import send_initial_emails
            leads = get_leads_for_email(limit=1000)
            if not leads:
                add_log("⚠ No leads pending email", "warning")
            else:
                sent = send_initial_emails(leads)
                agent_state["stats"]["emails_sent"] = sent
                add_log(f"✓ Sent {sent} initial emails")

        elif step_name == "forms":
            from modules.form_outreach import run_form_outreach
            result = run_form_outreach(batch_size=100)
            agent_state["stats"]["forms_filled"] = result.get("success", 0)
            add_log(f"✓ Forms: {result.get('success',0)} success, {result.get('no_form',0)} no form, {result.get('failed',0)} failed")

        elif step_name == "followup":
            from modules.database import get_followup_due
            from modules.emailer import send_followup_emails
            leads = get_followup_due()
            if not leads:
                add_log("⚠ No follow-ups due today", "warning")
            else:
                sent = send_followup_emails(leads)
                agent_state["stats"]["followups_sent"] = sent
                add_log(f"✓ Sent {sent} follow-up emails")

        elif step_name == "report":
            from modules.intelligence import generate_weekly_report, format_report_text
            report = generate_weekly_report()
            text = format_report_text(report)
            add_log(f"✓ Intelligence report generated")

        else:
            add_log(f"Unknown step: {step_name}", "error")

    except Exception as e:
        add_log(f"✗ Error in {step_name}: {str(e)}", "error")
        agent_state["errors"].append({"step": step_name, "error": str(e), "time": datetime.now(timezone.utc).isoformat()})

    agent_state["status"] = "idle"
    agent_state["current_step"] = None
    agent_state["last_run"] = datetime.now(timezone.utc).isoformat()



def _run_step_sync(step_name):
    """Run a step synchronously (used within the pipeline thread)."""
    try:
        if step_name == "scrape":
            from modules.scraper import run_daily_scrape
            leads = run_daily_scrape()
            agent_state["stats"]["leads_scraped"] = len(leads)
            agent_state["_temp_leads"] = leads
            add_log(f"✓ Scraped {len(leads)} leads (inserted into Supabase per-country)")
        elif step_name == "qualify":
            raw = agent_state.get("_temp_leads", [])
            if raw:
                try:
                    from modules.qualifier import qualify_leads
                    qualified = qualify_leads(raw, use_ai=True)
                    agent_state["stats"]["leads_qualified"] = len(qualified)
                except Exception as qe:
                    add_log(f"⚠ Qualifier unavailable ({qe}) — leads already stored unscored", "warning")
                    agent_state["stats"]["leads_qualified"] = len(raw)
            else:
                add_log("⚠ No leads to qualify")
        elif step_name == "store":
            # Leads are now stored in the scrape step — this is just a status check
            from modules.database import get_total_leads
            total = get_total_leads()
            add_log(f"✓ DB has {total} total leads")
        elif step_name == "email":
            from modules.database import get_leads_for_email
            from modules.emailer import send_initial_emails
            leads = get_leads_for_email(1000)
            sent = send_initial_emails(leads) if leads else 0
            agent_state["stats"]["emails_sent"] = sent
            add_log(f"✓ Sent {sent} emails")
        elif step_name == "forms":
            from modules.form_outreach import run_form_outreach
            result = run_form_outreach(batch_size=100)
            agent_state["stats"]["forms_filled"] = result.get("success", 0)
            add_log(f"✓ Forms: {result.get('success',0)} success, {result.get('failed',0)} failed")
        elif step_name == "followup":
            from modules.database import get_followup_due
            from modules.emailer import send_followup_emails
            leads = get_followup_due()
            sent = send_followup_emails(leads) if leads else 0
            agent_state["stats"]["followups_sent"] = sent
            add_log(f"✓ Sent {sent} follow-ups")
        elif step_name == "report":
            from modules.intelligence import generate_weekly_report
            generate_weekly_report()
            add_log(f"✓ Report generated")
    except Exception as e:
        add_log(f"✗ {step_name} error: {e}", "error")


# ═══════════════════════════════════════════════════════════════
# CONTINUOUS AGENT LOOP (runs forever until stopped)
# ═══════════════════════════════════════════════════════════════

_cycle_start_lock = threading.Lock()

_EMAIL_STOP_REASONS = (
    "daily_email_limit_reached", "quota_check_failed", "sender_capacity_ended",
    "all_senders_at_capacity", "no_sending_method_configured",
)


def start_daily_cycle(trigger: str = "manual") -> bool:
    """Start one daily cycle in the background. Returns False if one is already running."""
    with _cycle_start_lock:
        if agent_state["agent_loop_running"]:
            return False
        agent_state["agent_loop_running"] = True
    threading.Thread(target=agent_loop_thread, args=(trigger,), daemon=True, name="daily-cycle").start()
    return True


def _email_sending_configured() -> bool:
    import config
    from modules.email_queue import _using_smtp
    instantly_key = (getattr(config, "INSTANTLY_API_KEY", "") or "").strip()
    return _using_smtp() or bool(instantly_key and "your" not in instantly_key.lower())


def _run_email_phase(keep_running=None):
    """Email the oldest un-emailed leads without exceeding today's DAILY_EMAIL_LIMIT."""
    keep_running = keep_running or (lambda: agent_state["agent_loop_running"])
    import config
    from modules.database import get_leads_for_email, get_pending_email_count, get_emails_sent_today
    from modules.email_queue import process_lead_email, get_email_quota_remaining

    limit = int(config.DAILY_EMAIL_LIMIT)
    if not _email_sending_configured():
        add_log("⚠ Email skipped — no SMTP or Instantly account configured (Admin Panel → Email & SMTP)",
                "warning", category="email")
        return
    try:
        remaining = get_email_quota_remaining()
    except Exception as e:
        add_log(f"⚠ Email skipped — couldn't verify today's send count: {e}", "warning", category="email")
        return
    if remaining <= 0:
        add_log(f"⏸ Daily email limit reached ({limit}/{limit}) — remaining leads will be emailed next day",
                category="email")
        return

    pending = get_pending_email_count()
    leads = get_leads_for_email(limit=remaining * 3)  # extra room for leads skipped as invalid
    if not leads:
        add_log("✓ No un-emailed leads left", category="email")
        return
    add_log(f"▶ Emailing up to {remaining} of {pending} un-emailed leads (limit {limit}/day)", category="email")

    sent = 0
    consecutive_failures = 0
    for lead in leads:
        if not keep_running():
            break
        address = lead.get("contact_email", "?")
        success, reason = process_lead_email(lead, sequence_stage=1, return_reason=True)
        if reason == "sent":
            sent += 1
            consecutive_failures = 0
            add_log(f"  ✓ Email sent → {lead.get('company_name', '?')} ({address})", category="email")
            delay_min, delay_max = getattr(config, "EMAIL_SEND_DELAY", (2, 8))
            time.sleep(random.uniform(float(delay_min), float(delay_max)))
            continue
        if reason in _EMAIL_STOP_REASONS:
            add_log(f"⏸ Email phase stopped — {reason.replace('_', ' ')}", category="email")
            break
        add_log(f"  ⚠ Skipped {address} — {reason}", "warning", category="email")
        if reason.startswith("send_failed") or reason == "processing_error":
            consecutive_failures += 1
            if consecutive_failures >= 3:
                add_log("✗ 3 sends failed in a row — stopping so the lead queue isn't used up. "
                        "Check the SMTP credentials in the Admin Panel.", "error", category="email")
                break

    sent_today = get_emails_sent_today()
    agent_state["stats"]["emails_sent"] = sent_today
    add_log(f"✓ Email phase done — {sent} sent this run, {sent_today}/{limit} today, "
            f"{get_pending_email_count()} leads still to email", category="email")


def _run_form_phase(keep_running=None):
    """Submit contact forms on un-submitted leads without exceeding today's DAILY_FORM_LIMIT."""
    keep_running = keep_running or (lambda: agent_state["agent_loop_running"])
    import config
    from modules.database import get_pending_form_count, get_forms_submitted_today
    from modules.form_outreach import run_form_outreach, outreach_state
    from modules.form_filler import get_form_quota_remaining

    limit = int(config.DAILY_FORM_LIMIT)
    if outreach_state.get("running"):
        add_log("⚠ A form batch is already running from the Forms page — skipping form phase this cycle",
                "warning", category="form")
        return

    last_pending = None
    while keep_running():
        try:
            remaining = get_form_quota_remaining()
        except Exception as e:
            add_log(f"⚠ Form phase stopped — couldn't verify today's submission count: {e}", "warning", category="form")
            return
        if remaining <= 0:
            add_log(f"⏸ Daily form limit reached ({limit}/{limit}) — remaining leads continue next day", category="form")
            break
        pending = get_pending_form_count()
        if pending <= 0:
            add_log("✓ No un-submitted forms left", category="form")
            break
        if pending == last_pending:
            add_log("⚠ Form queue isn't shrinking — stopping form phase for this cycle", "warning", category="form")
            break
        last_pending = pending

        batch = min(remaining, pending, 10)
        add_log(f"  Form batch: {batch} sites ({remaining} submissions left today, {pending} in queue)", category="form")
        result = run_form_outreach(batch_size=batch) or {}
        if result.get("error"):
            add_log(f"⚠ Form batch error: {result['error']}", "error", category="form")
            break
        agent_state["stats"]["forms_filled"] = get_forms_submitted_today()

    add_log(f"✓ Form phase done — {get_forms_submitted_today()}/{limit} submitted today, "
            f"{get_pending_form_count()} forms still pending", category="form")


def agent_loop_thread(trigger: str = "manual"):
    """
    One daily cycle:
      1. Queue check — if any lead is still un-emailed or its form un-submitted, skip extraction
         and keep working through those leads. Only when both queues are empty, extract a fresh
         batch of quality-gated leads.
      2. Email — at most DAILY_EMAIL_LIMIT sends per day.
      3. Forms — at most DAILY_FORM_LIMIT successful submissions per day.
      4. Summary.
    Caps are enforced again inside process_lead_email / run_form_filling, so manual
    dashboard actions can't exceed them either.
    """
    import config
    from modules.database import get_total_leads, get_daily_quota_status, release_stuck_form_leads
    from modules.form_outreach import outreach_state

    agent_state["agent_loop_running"] = True
    agent_state["pipeline_running"] = True
    agent_state["status"] = "running"
    agent_state["started_at"] = datetime.now(timezone.utc).isoformat()
    agent_state["cycle"] = int(agent_state.get("cycle") or 0) + 1
    agent_state["completed_phases"] = []
    add_log(f"Daily cycle started ({trigger})", category="system")

    def _phase_pause(seconds=3):
        for _ in range(seconds):
            if not agent_state["agent_loop_running"]:
                return False
            time.sleep(1)
        return True

    completed = False
    try:
        # ── PHASE 1: queue check → extract only if nothing is left ──
        agent_state["current_step"] = "store"
        if not outreach_state.get("running"):
            released = release_stuck_form_leads()
            if released:
                add_log(f"Returned {released} interrupted form submissions to the queue", category="form")

        quota = get_daily_quota_status()
        _cache_quota(quota)
        if not quota["extraction_needed"]:
            add_log(
                f"✓ {quota['pending_email']} leads not yet emailed, {quota['pending_form']} forms not yet "
                "submitted — skipping extraction and continuing with existing leads",
                category="lead",
            )
        else:
            add_log(f"No untouched leads left — extracting up to {config.DAILY_LEAD_TARGET} top-quality leads",
                    category="lead")
            agent_state["current_step"] = "scrape"
            before = get_total_leads()
            _run_step_sync("scrape")
            added = max(0, get_total_leads() - before)
            quota = get_daily_quota_status()
            _cache_quota(quota)
            add_log(f"✓ Extraction finished — {added} new leads stored "
                    f"({quota['pending_email']} to email, {quota['pending_form']} forms to submit)", category="lead")
            agent_state["current_step"] = "store"

        agent_state["stats"]["leads_stored"] = get_total_leads()
        agent_state["completed_phases"].append("store")
        if not _phase_pause():
            return

        # ── PHASE 2: email ──
        agent_state["current_step"] = "email"
        _run_email_phase()
        agent_state["completed_phases"].append("email")
        if not _phase_pause():
            return

        # ── PHASE 3: forms ──
        agent_state["current_step"] = "forms"
        _run_form_phase()
        agent_state["completed_phases"].append("forms")
        if not _phase_pause():
            return

        # ── PHASE 4: summary ──
        agent_state["current_step"] = "report"
        quota = get_daily_quota_status()
        _cache_quota(quota)
        add_log(
            f"📊 Today: {quota['emails_sent_today']}/{quota['email_limit']} emails, "
            f"{quota['forms_submitted_today']}/{quota['form_limit']} forms | "
            f"Still queued: {quota['pending_email']} to email, {quota['pending_form']} forms",
            category="system",
        )
        if quota["extraction_needed"]:
            add_log("All leads have been contacted — the next cycle will extract a fresh batch", category="system")
        agent_state["completed_phases"].append("report")
        completed = True
    except Exception as e:
        add_log(f"✗ Daily cycle error: {e}", "error", category="system")
        agent_state["errors"].append({"step": "daily_cycle", "error": str(e), "time": datetime.now(timezone.utc).isoformat()})
    finally:
        agent_state["current_step"] = None
        agent_state["last_run"] = datetime.now(timezone.utc).isoformat()
        if completed:
            agent_state["status"] = "completed"
            add_log("✅ Daily cycle complete", category="system")
            time.sleep(5)  # let the dashboard show the completed state
        _agent_cleanup()


# ═══════════════════════════════════════════════════════════════
# DAILY SCHEDULER + QUOTA CACHE
# ═══════════════════════════════════════════════════════════════

_quota_cache = {"at": 0.0, "data": None}


def _cache_quota(data: dict):
    _quota_cache["at"] = time.time()
    _quota_cache["data"] = data


def get_cached_quota(max_age: float = 15.0) -> dict:
    """Quota status for dashboard polling without hammering the database."""
    if _quota_cache["data"] is None or time.time() - _quota_cache["at"] > max_age:
        try:
            from modules.database import get_daily_quota_status
            _cache_quota(get_daily_quota_status())
        except Exception as e:
            logger.warning(f"Quota status unavailable: {e}")
    return _quota_cache["data"] or {}


def daily_scheduler_thread():
    """Start the daily cycle once per business day, after DAILY_RUN_HOUR:DAILY_RUN_MINUTE local time."""
    import config
    last_started_day = None
    while True:
        try:
            if getattr(config, "AUTO_DAILY_RUN", True):
                now_local = datetime.now(timezone.utc).astimezone()
                run_at = now_local.replace(hour=config.DAILY_RUN_HOUR, minute=config.DAILY_RUN_MINUTE,
                                           second=0, microsecond=0)
                day = _current_business_date_str(config.BUSINESS_DAY_RESET_HOUR)
                if day != last_started_day and now_local >= run_at:
                    if start_daily_cycle(trigger="auto daily run"):
                        last_started_day = day
        except Exception as e:
            logger.error(f"[Scheduler] {e}")
        time.sleep(60)


def _agent_cleanup():
    """Clean up agent state after completion or stop."""
    try:
        from modules.email_queue import stop_email_workers
        stop_email_workers()
    except:
        pass
    agent_state["agent_loop_running"] = False
    agent_state["pipeline_running"] = False
    agent_state["status"] = "idle"
    agent_state["current_step"] = None
    add_log("Agent stopped — workers halted", category="system")


# ═══════════════════════════════════════════════════════════════
# AI CHAT (uses Claude to answer questions about the agent)
# ═══════════════════════════════════════════════════════════════

def _get_db_context() -> str:
    """Pull comprehensive database stats for AI context."""
    from modules.database import db
    if not db:
        return "DATABASE: Not connected"

    lines = []
    try:
        total = db.count("leads")
        emailed = db.count("leads", {"email_sent": "eq.true"})
        replied = db.count("leads", {"replied": "eq.true"})
        interested = db.count("leads", {"interested": "eq.true"})
        closed = db.count("leads", {"closed": "eq.true"})
        opened = db.count("leads", {"email_opened": "eq.true"})
        lines.append(f"TOTALS: {total} leads, {emailed} emailed, {opened} opened, {replied} replied, {interested} interested, {closed} closed")
    except:
        lines.append("TOTALS: unavailable")

    # Country breakdown
    try:
        all_leads = db.select("leads", columns="country", limit=5000)
        country_counts = {}
        for l in all_leads:
            c = l.get("country", "Unknown")
            country_counts[c] = country_counts.get(c, 0) + 1
        top_countries = sorted(country_counts.items(), key=lambda x: -x[1])[:15]
        lines.append("LEADS BY COUNTRY: " + ", ".join(f"{c}: {n}" for c, n in top_countries))
    except:
        pass

    # Lead type breakdown
    try:
        type_counts = {}
        for l in all_leads:
            t = l.get("lead_type", "other")
            type_counts[t] = type_counts.get(t, 0) + 1
        lines.append("LEADS BY TYPE: " + ", ".join(f"{t}: {n}" for t, n in sorted(type_counts.items(), key=lambda x: -x[1])))
    except:
        pass

    # Email tracking stats
    try:
        tracking = db.select("email_tracking_stats", limit=1)
        if tracking:
            t = tracking[0]
            lines.append(f"EMAIL TRACKING: {t.get('total_tracked',0)} tracked, {t.get('total_opened',0)} opened, {t.get('unique_opens',0)} unique opens, {t.get('open_rate',0)}% open rate")
    except:
        pass

    # Source tracker
    try:
        sources = db.select("source_tracker", columns="source,country,total_found,status", order="total_found.desc", limit=20)
        if sources:
            source_summary = {}
            for s in sources:
                src = s.get("source", "?")
                source_summary[src] = source_summary.get(src, 0) + (s.get("total_found", 0) or 0)
            lines.append("LEADS BY SOURCE: " + ", ".join(f"{s}: {n}" for s, n in sorted(source_summary.items(), key=lambda x: -x[1])))
    except:
        pass

    # Top scoring leads
    try:
        hot = db.select("leads", columns="company_name,country,score,contact_email,replied,interested",
                        filters={"score": "gte.70", "email_sent": "eq.true"},
                        order="score.desc", limit=10)
        if hot:
            lines.append("TOP SCORED EMAILED LEADS: " + "; ".join(
                f"{l['company_name']} ({l['country']}, score:{l['score']}, replied:{l.get('replied',False)})" for l in hot
            ))
    except:
        pass

    # Segment performance
    try:
        segments = db.select("segment_performance", order="close_rate.desc", limit=10)
        if segments:
            lines.append("SEGMENT PERFORMANCE: " + "; ".join(
                f"{s['segment_type']}/{s['segment_value']}: {s['total_leads']} leads, {s.get('replies',0)} replies, {s.get('closed',0)} closed, paused={s.get('is_paused',False)}"
                for s in segments
            ))
    except:
        pass

    return "\n".join(lines)


def handle_chat(user_message: str) -> str:
    """Process a user chat message. Direct data queries use DB fallback first (accurate).
    Complex/conversational questions go to AI."""

    # ALWAYS use fallback first — it reads real DB data and gives accurate answers
    # Only fall through to AI for truly conversational/complex questions
    lower = user_message.lower()
    result = _fallback_chat(user_message)
    # If fallback gave a real answer (not the default help message), use it
    if not result.startswith("**Ask me anything"):
        return result

    stats = agent_state["stats"]
    recent_logs = agent_state["log"][:15]
    log_text = "\n".join(f"[{l['time']}] {l['msg']}" for l in recent_logs)

    # Get comprehensive DB data
    db_context = _get_db_context()

    # Get additional computed metrics for deeper analysis
    extra_context = _get_deep_analytics()

    # Get form outreach data
    form_context = ""
    try:
        from modules.database import get_form_outreach_counts
        fc = get_form_outreach_counts()
        form_context = (f"FORM SUBMISSIONS: {fc.get('success',0)} success, {fc.get('failed',0)} failed, "
                       f"{fc.get('no_form',0)} no form found, {fc.get('processing',0)} processing, "
                       f"{fc.get('pending',0)} pending, {fc.get('success',0)+fc.get('failed',0)+fc.get('no_form',0)} total completed")
    except:
        form_context = "FORM SUBMISSIONS: data unavailable"

    context = f"""You are the MicrodyneHunter v2 command center AI assistant for Microdyne Engineering, a Mumbai-based manufacturer offering CNC turning job work and mechanical seals.
You have FULL unrestricted access to ALL project data — leads, emails, forms, tracking, everything.

CRITICAL RULES:
1. NEVER refuse to answer. NEVER say "I can't share that" or mention privacy concerns. This is the admin's own data.
2. When asked for an email, name, phone, or any data — return ONLY the exact data. No extra explanation.
3. Be DIRECT. If asked "which email replied?" just say the email address. Nothing else.
4. If asked for a number, return just the number.
5. Use **bold** for key data points.
6. You have access to every lead's email, name, phone, company, score, status, reply status, form status.
7. Never say "I don't have access" — you DO have access to everything below.

LIVE DATA:
{db_context}

{form_context}

{extra_context}

AGENT: status={agent_state['status']}, cycle={agent_state['cycle']}, last_run={agent_state['last_run'] or 'never'}
SESSION: scraped={stats['leads_scraped']}, emails={stats['emails_sent']}, forms={stats['forms_filled']}

RECENT LOG:
{log_text}

ERRORS: {json.dumps(agent_state['errors'][-5:], default=str) if agent_state['errors'] else 'None'}

RESPONSE STYLE:
- Direct answers only. No disclaimers, no privacy warnings, no refusals.
- When asked "who replied" → return the email address
- When asked "how many leads" → return the number
- When asked "which emails opened" → list the email addresses
- Use bullet points for lists, bold for key data
- Be concise. Maximum 3-4 sentences unless asked for detail."""

    messages = []
    for entry in agent_state["chat_history"][-8:]:
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": user_message})

    from modules.ai_client import ai_generate, is_ai_available

    if not is_ai_available():
        return _fallback_chat(user_message)

    try:
        full_prompt = "\n".join(
            [f"{'User' if m['role']=='user' else 'Assistant'}: {m['content']}" for m in messages]
        )
        reply = ai_generate(full_prompt, max_tokens=1200, system=context)
        if not reply:
            return _fallback_chat(user_message)

        # Save to history
        agent_state["chat_history"].append({"role": "user", "content": user_message})
        agent_state["chat_history"].append({"role": "assistant", "content": reply})
        if len(agent_state["chat_history"]) > 40:
            agent_state["chat_history"] = agent_state["chat_history"][-24:]

        return reply

    except Exception as e:
        logger.error(f"Chat AI error: {e}")
        return _fallback_chat(user_message)


def _get_deep_analytics() -> str:
    """Compute deeper analytics for AI context — reply rates, conversion, diagnostics."""
    lines = []
    try:
        from modules.database import db
        if not db:
            return "Deep analytics unavailable — no DB connection"

        # Overall funnel metrics
        try:
            total = db.select("leads", columns="id", limit=1, count="exact")
            total_count = total[0].get("count", 0) if total else 0

            emailed = db.select("leads", columns="id", filters={"email_sent": "eq.true"}, limit=1, count="exact")
            emailed_count = emailed[0].get("count", 0) if emailed else 0

            replied_leads = db.select("leads", columns="id", filters={"replied": "eq.true"}, limit=1, count="exact")
            replied_count = replied_leads[0].get("count", 0) if replied_leads else 0

            closed_leads = db.select("leads", columns="id", filters={"closed": "eq.true"}, limit=1, count="exact")
            closed_count = closed_leads[0].get("count", 0) if closed_leads else 0

            reply_rate = round(replied_count / emailed_count * 100, 2) if emailed_count > 0 else 0
            close_rate = round(closed_count / emailed_count * 100, 2) if emailed_count > 0 else 0

            lines.append(f"FUNNEL: {total_count} total leads → {emailed_count} emailed → {replied_count} replied ({reply_rate}%) → {closed_count} closed ({close_rate}%)")
        except:
            pass

        # Reply rate by country
        try:
            all_leads = db.select("leads", columns="country,email_sent,replied,closed,score", limit=5000)
            if all_leads:
                country_stats = {}
                for l in all_leads:
                    c = l.get("country", "Unknown")
                    if c not in country_stats:
                        country_stats[c] = {"total": 0, "emailed": 0, "replied": 0, "closed": 0, "scores": []}
                    country_stats[c]["total"] += 1
                    if l.get("email_sent"): country_stats[c]["emailed"] += 1
                    if l.get("replied"): country_stats[c]["replied"] += 1
                    if l.get("closed"): country_stats[c]["closed"] += 1
                    if l.get("score"): country_stats[c]["scores"].append(l["score"])

                country_lines = []
                for c, s in sorted(country_stats.items(), key=lambda x: -x[1]["total"]):
                    rr = round(s["replied"] / s["emailed"] * 100, 1) if s["emailed"] > 0 else 0
                    cr = round(s["closed"] / s["emailed"] * 100, 1) if s["emailed"] > 0 else 0
                    avg_score = round(sum(s["scores"]) / len(s["scores"]), 1) if s["scores"] else 0
                    country_lines.append(f"{c}: {s['total']} leads, {s['emailed']} emailed, {rr}% reply, {cr}% close, avg_score={avg_score}")
                lines.append("COUNTRY PERFORMANCE:\n  " + "\n  ".join(country_lines[:12]))

                # Lead type performance
                type_stats = {}
                for l in all_leads:
                    t = l.get("lead_type", "other")
                    if t not in type_stats:
                        type_stats[t] = {"total": 0, "emailed": 0, "replied": 0, "closed": 0}
                    type_stats[t]["total"] += 1
                    if l.get("email_sent"): type_stats[t]["emailed"] += 1
                    if l.get("replied"): type_stats[t]["replied"] += 1
                    if l.get("closed"): type_stats[t]["closed"] += 1

                type_lines = []
                for t, s in sorted(type_stats.items(), key=lambda x: -x[1]["total"]):
                    rr = round(s["replied"] / s["emailed"] * 100, 1) if s["emailed"] > 0 else 0
                    type_lines.append(f"{t}: {s['total']} leads, {s['emailed']} emailed, {rr}% reply, {s['closed']} closed")
                lines.append("LEAD TYPE PERFORMANCE:\n  " + "\n  ".join(type_lines))
        except:
            pass

        # Domain health
        try:
            outreach = db.select("outreach_log", columns="sending_domain,delivery_status", limit=5000)
            if outreach:
                domain_stats = {}
                for o in outreach:
                    d = o.get("sending_domain", "unknown")
                    if d not in domain_stats:
                        domain_stats[d] = {"sent": 0, "failed": 0, "bounced": 0}
                    domain_stats[d]["sent"] += 1
                    if o.get("delivery_status") == "failed": domain_stats[d]["failed"] += 1
                    if o.get("delivery_status") == "bounced": domain_stats[d]["bounced"] += 1
                domain_lines = []
                for d, s in sorted(domain_stats.items(), key=lambda x: -x[1]["sent"]):
                    fail_rate = round((s["failed"] + s["bounced"]) / s["sent"] * 100, 1) if s["sent"] > 0 else 0
                    domain_lines.append(f"{d}: {s['sent']} sent, {s['failed']} failed, {s['bounced']} bounced ({fail_rate}% fail)")
                lines.append("DOMAIN SEND STATS:\n  " + "\n  ".join(domain_lines))
        except:
            pass

        # Paused segments
        try:
            paused = db.select("segment_performance", filters={"is_paused": "eq.true"}, limit=50)
            if paused:
                lines.append("PAUSED SEGMENTS: " + "; ".join(
                    f"{s['segment_type']}/{s['segment_value']} (reason: {s.get('pause_reason','unknown')})" for s in paused
                ))
            else:
                lines.append("PAUSED SEGMENTS: None currently paused")
        except:
            pass

    except Exception as e:
        lines.append(f"Analytics error: {str(e)}")

    return "\n".join(lines)


def _fallback_chat(msg: str) -> str:
    """Smart fallback — pulls real DB data and gives direct answers."""
    lower = msg.lower()
    stats = agent_state["stats"]

    # Load full lead data for answering any question
    all_leads = []
    db_nums = {"total": 0, "emailed": 0, "replied": 0, "closed": 0, "opened": 0, "reply_rate": "0", "forms": 0, "form_success": 0, "form_failed": 0}
    try:
        from modules.database import db, get_form_outreach_counts
        if db:
            all_leads = db.select("leads", columns="company_name,contact_email,contact_name,contact_phone,country,lead_type,score,email_sent,email_opened,replied,replied_at,reply_source,form_filled,closed,website_url,sending_domain", limit=10000)
            db_nums["total"] = len(all_leads)
            db_nums["emailed"] = sum(1 for l in all_leads if l.get("email_sent"))
            db_nums["replied"] = sum(1 for l in all_leads if l.get("replied"))
            db_nums["opened"] = sum(1 for l in all_leads if l.get("email_opened"))
            db_nums["closed"] = sum(1 for l in all_leads if l.get("closed"))
            if db_nums["emailed"] > 0:
                db_nums["reply_rate"] = str(round(db_nums["replied"] / db_nums["emailed"] * 100, 1))
            try:
                fc = get_form_outreach_counts()
                db_nums["form_success"] = fc.get("success", 0)
                db_nums["form_failed"] = (fc.get("failed", 0) or 0) + (fc.get("no_form", 0) or 0)
                db_nums["forms"] = db_nums["form_success"] + db_nums["form_failed"]
            except:
                pass
    except:
        pass

    # PROJECT IDENTITY — answer about the app/project itself
    if ("project" in lower and "name" in lower) or ("app" in lower and "name" in lower) or ("what is this" in lower):
        return "**MicrodyneHunter v2** — AI Sales Agent for Microdyne Engineering"

    if "who made" in lower or "who built" in lower or "who created" in lower or "developer" in lower:
        return "**MicrodyneHunter v2** — built for Microdyne Engineering, Mumbai, India"

    if ("company" in lower and "name" in lower) and ("my" in lower or "our" in lower):
        return "**Microdyne Engineering** — Mechanical Seals & Hydraulic Fittings, Mumbai"

    if "version" in lower:
        return "**MicrodyneHunter v2.0**"

    # DIRECT DATA QUERIES — return exact data, no fluff
    # Who replied / which email replied
    if ("who" in lower or "which" in lower) and "repl" in lower:
        replied_leads = [l for l in all_leads if l.get("replied")]
        if replied_leads:
            emails = "\n".join(f"• **{l['contact_email']}** ({l.get('company_name','?')})" for l in replied_leads)
            return f"{emails}"
        return "No replies yet."

    # Who opened / which email opened
    if ("who" in lower or "which" in lower) and "open" in lower:
        opened_leads = [l for l in all_leads if l.get("email_opened")]
        if opened_leads:
            emails = "\n".join(f"• **{l['contact_email']}** ({l.get('company_name','?')})" for l in opened_leads)
            return f"{emails}"
        return "No opens tracked yet."

    # Which email was sent / who was emailed
    if ("who" in lower or "which" in lower) and ("sent" in lower or "email" in lower):
        emailed = [l for l in all_leads if l.get("email_sent")]
        if emailed:
            emails = "\n".join(f"• **{l['contact_email']}** ({l.get('company_name','?')}) — score: {l.get('score',0)}" for l in emailed)
            return f"{emails}"
        return "No emails sent yet."

    # Reply from / reply email
    if "reply" in lower and ("from" in lower or "email" in lower or "who" in lower):
        replied_leads = [l for l in all_leads if l.get("replied")]
        if replied_leads:
            return "\n".join(f"**{l['contact_email']}**" for l in replied_leads)
        return "No replies yet."

    # Hot leads — MUST be before "how many leads"
    if "hot" in lower and "lead" in lower:
        from modules.database import get_hot_leads
        hot = get_hot_leads()
        if hot:
            rows = []
            for i, l in enumerate(hot[:15], 1):
                status_lbl = "REPLIED" if l.get("replied") else "OPENED" if l.get("email_opened") else "INTERESTED" if l.get("interested") else "NEW"
                rows.append(f"**{i}. {l.get('company_name','?')}**\n   Email: {l.get('contact_email','?')} | Score: **{l.get('score',0)}** | Country: {l.get('country','?')} | Status: {status_lbl}")
            return f"**{len(hot)} Hot Leads:**\n\n" + "\n\n".join(rows)
        return "**0 hot leads** right now. Hot leads = replied, opened with score 70+, or interested."

    # Status / how's it going
    if "status" in lower or ("how" in lower and "going" in lower):
        status = f"running (cycle {agent_state['cycle']})" if agent_state["agent_loop_running"] else "idle"
        return (f"**Agent: {status.upper()}**\n"
                f"• Leads: **{db_nums['total']:,}** | Emailed: **{db_nums['emailed']:,}** | Opened: **{db_nums['opened']:,}**\n"
                f"• Replied: **{db_nums['replied']:,}** ({db_nums['reply_rate']}%) | Forms: **{db_nums['forms']:,}**\n"
                f"• Closed: **{db_nums['closed']:,}**")

    # How many leads / total leads
    if ("total" in lower or "how many" in lower or "count" in lower) and "lead" in lower:
        return f"**{db_nums['total']:,}** leads"

    # How many emails sent
    if ("how many" in lower or "total" in lower) and ("email" in lower or "sent" in lower):
        return f"**{db_nums['emailed']:,}** emails sent"

    # How many forms
    if ("how many" in lower or "total" in lower) and "form" in lower:
        return f"**{db_nums['forms']:,}** forms ({db_nums['form_success']} success, {db_nums['form_failed']} failed)"

    # How many replies
    if ("how many" in lower or "total" in lower) and "repl" in lower:
        return f"**{db_nums['replied']:,}** replies ({db_nums['reply_rate']}% rate)"

    # How many opened
    if ("how many" in lower or "total" in lower) and "open" in lower:
        return f"**{db_nums['opened']:,}** emails opened"

    # List all leads
    if "list" in lower and "lead" in lower:
        if all_leads:
            rows = "\n".join(f"• **{l['company_name']}** — {l.get('contact_email','?')} ({l.get('country','?')}) score={l.get('score',0)}" for l in all_leads[:20])
            return f"**{db_nums['total']} leads:**\n{rows}"
        return "No leads in database."

    # Form status
    if "form" in lower:
        return (f"**Forms:** {db_nums['form_success']} success, {db_nums['form_failed']} failed, {db_nums['forms']} total")

    # Email stats
    if "email" in lower:
        return (f"**Emails:** {db_nums['emailed']} sent, {db_nums['opened']} opened, {db_nums['replied']} replied ({db_nums['reply_rate']}% rate)")

    # Error check
    if "error" in lower:
        errors = agent_state["errors"][-5:] if agent_state["errors"] else []
        if errors:
            return "\n".join(f"• {e.get('msg','?')}" for e in errors)
        return "No errors."

    # Dashboard / sections / features
    if "dashboard" in lower or "section" in lower or "feature" in lower or "what" in lower and ("have" in lower or "app" in lower or "this" in lower):
        return ("**Yes! This app has these sections:**\n"
                "• **Dashboard** — Overview with stats, pipeline phases, hot leads, live activity\n"
                "• **Leads** — Full lead database with filters (country, type, score, date)\n"
                "• **Email** — Email outreach, domain health, sequence tracker, open tracking\n"
                "• **Forms** — Contact form filling with batch controls and status tracking\n"
                "• **Reports** — Pipeline report with filters, export PDF/CSV\n"
                "• **Sources** — Keyword/source tracking for lead scraping\n"
                "• **Funnel** — Sales funnel analytics with charts\n"
                "• **Pipeline** — Phase flow visualization (Store → Email → Forms → Report)\n"
                "• **Admin Panel** — API keys, email config, form content, credentials (/admin)")

    # Hot leads
    # (hot leads already handled above)

    # Report / PDF
    if "report" in lower or "pdf" in lower or "summary" in lower:
        # Build full report data
        emailed_leads = [l for l in all_leads if l.get("email_sent")]
        formed_leads = [l for l in all_leads if l.get("form_filled")]
        opened_leads = [l for l in all_leads if l.get("email_opened")]
        replied_leads = [l for l in all_leads if l.get("replied")]
        outreached = [l for l in all_leads if l.get("email_sent") or l.get("form_filled")]

        report_text = f"**Pipeline Report**\n\n"
        report_text += f"• Total Leads: **{db_nums['total']}**\n"
        report_text += f"• Emails Sent: **{db_nums['emailed']}** | Opened: **{db_nums['opened']}** | Replied: **{db_nums['replied']}**\n"
        report_text += f"• Forms Filled: **{db_nums['form_success']}**\n"
        report_text += f"• Open Rate: **{round(db_nums['opened']/db_nums['emailed']*100,1) if db_nums['emailed'] else 0}%** | Reply Rate: **{db_nums['reply_rate']}%**\n\n"

        if outreached:
            report_text += "**Outreach Details:**\n"
            for i, l in enumerate(outreached[:20], 1):
                e_status = "Sent" if l.get("email_sent") else "-"
                o_status = "Opened" if l.get("email_opened") else "-"
                f_status = "Filled" if l.get("form_filled") else "-"
                r_status = "Replied" if l.get("replied") else "-"
                report_text += f"{i}. **{l.get('company_name','?')}** | {l.get('contact_email','?')} | Email: {e_status} | Open: {o_status} | Form: {f_status} | Reply: {r_status}\n"

        if "pdf" in lower or "download" in lower or "convert" in lower:
            report_text += "\n\n[PDF_DOWNLOAD]"

        return report_text

    # Pipeline / agent / phases
    if "pipeline" in lower or "phase" in lower or "agent" in lower:
        status = "running" if agent_state["agent_loop_running"] else "idle"
        phases = agent_state.get("completed_phases", [])
        phase_text = ", ".join(phases) if phases else "none"
        return (f"**Pipeline: {status.upper()}**\n"
                f"• Phases: Store → Email → Forms → Report\n"
                f"• Completed: {phase_text}\n"
                f"• Current: {agent_state.get('current_step') or 'none'}\n"
                f"• Last run: {agent_state.get('last_run') or 'never'}")

    # Score / scoring
    if "score" in lower or "scoring" in lower:
        high = sum(1 for l in all_leads if (l.get("score") or 0) >= 70)
        mid = sum(1 for l in all_leads if 40 <= (l.get("score") or 0) < 70)
        low = sum(1 for l in all_leads if (l.get("score") or 0) < 40)
        return f"**Lead Scores:** {high} high (70+), {mid} qualified (40-69), {low} low (<40)\nMin score for outreach: **40**"

    # Country
    if "country" in lower or "countries" in lower:
        countries = {}
        for l in all_leads:
            c = l.get("country", "Unknown")
            countries[c] = countries.get(c, 0) + 1
        if countries:
            rows = "\n".join(f"• **{c}**: {n} leads" for c, n in sorted(countries.items(), key=lambda x: -x[1])[:10])
            return f"**Leads by country:**\n{rows}"
        return "No country data."

    # Domain / sending
    if "domain" in lower or "sending" in lower:
        import config
        emails = getattr(config, 'SENDING_EMAILS', config.SENDING_DOMAINS)
        return f"**Sending accounts:** {', '.join(emails)}"

    # Keywords
    if "keyword" in lower:
        import config
        kws = config.SEARCH_KEYWORDS[:5]
        return f"**Search keywords:** " + ", ".join(kws) + f" (+{max(0, len(config.SEARCH_KEYWORDS)-5)} more)"

    # Start / run
    if "start" in lower or "run" in lower:
        if agent_state["agent_loop_running"]:
            return f"**Agent already running** — cycle {agent_state['cycle']}"
        return "**Agent is idle.** Click START AGENT on the dashboard to run the pipeline."

    # Stop
    if "stop" in lower:
        return "**Agent is idle.** Nothing to stop." if not agent_state["agent_loop_running"] else "Click STOP on the dashboard."

    # Help
    if "help" in lower:
        return ("**I can answer:**\n"
                "• who replied / opened / was emailed\n"
                "• how many leads / emails / forms\n"
                "• hot leads\n"
                "• status / pipeline / phases\n"
                "• dashboard sections\n"
                "• scores / countries / domains / keywords\n"
                "• list leads\n"
                "• errors")

    # Default — show what I can answer
    return (f"**Ask me anything about your data:**\n"
            f"• \"who replied?\" → email addresses\n"
            f"• \"which email opened?\" → opened leads\n"
            f"• \"how many leads?\" → count\n"
            f"• \"hot leads\" → priority leads\n"
            f"• \"dashboard\" → all sections\n"
            f"• \"status\" → pipeline report\n\n"
            f"**Live:** {db_nums['total']} leads | {db_nums['emailed']} emailed | {db_nums['replied']} replied | {db_nums['forms']} forms")


# ═══════════════════════════════════════════════════════════════
# DATABASE QUERIES FOR DASHBOARD
# ═══════════════════════════════════════════════════════════════

_last_reply_check = 0  # timestamp of last reply check


def _parse_ymd(value: str):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _in_date_range(ts_value: str, date_from: str = "", date_to: str = "") -> bool:
    if not ts_value:
        return False
    try:
        d = datetime.fromisoformat(ts_value.replace("Z", "+00:00")).astimezone().date()
    except Exception:
        try:
            d = datetime.fromisoformat(ts_value[:19]).date()
        except Exception:
            return False

    f = _parse_ymd(date_from) if date_from else None
    t = _parse_ymd(date_to) if date_to else None
    if f and d < f:
        return False
    if t and d > t:
        return False
    return True


def _date_and(field: str, date_from: str = "", date_to: str = "") -> str:
    """
    Build Supabase `and` filter for YYYY-MM-DD inclusive range on a timestamp field,
    interpreting the dates in LOCAL timezone (so UI day matches user’s locale).
    """
    def _local_bounds(ymd: str, end: bool = False) -> str:
        d = _parse_ymd(ymd)
        if not d:
            return ""
        now_local = datetime.now(timezone.utc).astimezone()
        tz = now_local.tzinfo
        if end:
            local_dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz)
        else:
            local_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
        return local_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    parts = []
    if date_from:
        start_utc = _local_bounds(date_from, end=False)
        if start_utc:
            parts.append(f"{field}.gte.{start_utc}")
    if date_to:
        end_utc = _local_bounds(date_to, end=True)
        if end_utc:
            parts.append(f"{field}.lte.{end_utc}")
    return "(" + ",".join(parts) + ")" if parts else "()"

def _is_hidden(l: dict) -> bool:
    """Check if lead should be hidden from UI (processed > 24h ago)."""
    now = datetime.now(timezone.utc)
    # Check email
    if l.get("email_sent"):
        email_at = l.get("email_sent_at")
        if email_at:
            try:
                dt = datetime.fromisoformat(email_at.replace("Z", "+00:00"))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                if now - dt > timedelta(hours=24): return True
            except Exception: pass
    # Check form
    status = l.get("form_submission_status", "pending")
    if status != "pending":
        form_at = l.get("form_last_attempted_at")
        if form_at:
            try:
                dt = datetime.fromisoformat(form_at.replace("Z", "+00:00"))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                if now - dt > timedelta(hours=24): return True
            except Exception: pass
    return False

def get_dashboard_data():
    """Get all data the dashboard needs."""
    global _last_reply_check
    from modules.database import db, get_total_leads, get_today_stats, get_hot_leads
    try:
        _daily_business_reset_if_needed()
        if not agent_state.get("initial_sync_done"):
            _sync_existing_lead_records()
            agent_state["initial_sync_done"] = True
        notice = agent_state.get("daily_reset_notice")
        if notice and notice.get("visible_until") and time.time() > float(notice.get("visible_until")):
            agent_state["daily_reset_notice"] = None
        import config
        # Auto-sync email_opened from tracking table
        if db:
            try:
                opened_tracks = db.select("email_tracking", filters={"opened": "eq.true"}, columns="lead_id", limit=500)
                for t in opened_tracks:
                    if t.get("lead_id"):
                        db.update("leads", {"email_opened": True}, {"id": f"eq.{t['lead_id']}"})
            except:
                pass

        # Auto-check replies every 15 seconds (triggered by dashboard polling)
        now_ts = time.time()
        if now_ts - _last_reply_check >= 15:
            _last_reply_check = now_ts
            try:
                # Check if there are any emailed but unreplied leads
                # Check email replies
                unreplied = db.count("leads", {"email_sent": "eq.true", "replied": "eq.false"}) if db else 0
                if unreplied > 0:
                    from modules.reply_tracker import check_replies
                    count = check_replies()
                    if count > 0:
                        add_log(f"Email reply detected: {count} new replies", category="email")

                # Check form submission replies (domain matching)
                unreplied_forms = db.count("leads", {"form_filled": "eq.true", "replied": "eq.false"}) if db else 0
                if unreplied_forms > 0:
                    from modules.reply_tracker import check_form_replies
                    form_count = check_form_replies()
                    if form_count > 0:
                        add_log(f"Form reply detected: {form_count} new replies", category="form")
            except Exception as e:
                logger.error(f"Auto reply check error: {e}")

        from modules.database import get_business_day_range
        total = get_total_leads()
        today = get_today_stats()
        _, start_utc, end_utc = get_business_day_range(reset_hour_local=11)
        # Replies today (business-day window) for the "yesterday/today" sublines
        try:
            today["replies"] = int(db.count("leads", {"and": f"(replied.eq.true,replied_at.gte.{start_utc},replied_at.lt.{end_utc})"}) or 0) if db else 0
        except Exception:
            today["replies"] = 0
        hot = get_hot_leads()

        min_score = getattr(config, "SCORE_THRESHOLDS", {}).get("min_qualify", 40)
        active_lead_filters = {
            "sequence_stage": "eq.0",
            "email_sent": "eq.false",
            "excluded": "eq.false",
            "score": f"gte.{min_score}",
        }
        all_leads_raw = db.select("leads", filters=active_lead_filters, order="created_at.desc", limit=1000) if db else []
        leads = (all_leads_raw or [])[:500]

        # Batch counters: persist across days until pending queue hits 0.
        _ensure_batch_state_loaded()
        bs = agent_state.get("batch_state") or {}
        batch_active = bool(bs.get("active"))
        batch_start = (bs.get("start_utc_iso") or "")
        if batch_active and batch_start:
            emailed = int(db.count("outreach_log", {"and": f"(channel.eq.email,sent_at.gte.{batch_start})"}) or 0)
            forms = int(db.count("leads", {"and": f"(form_last_attempted_at.gte.{batch_start},form_submission_status.neq.pending)"}) or 0)
            replies = int(db.count("leads", {"and": f"(replied.eq.true,replied_at.gte.{batch_start})"}) or 0)
        else:
            emailed = 0
            forms = 0
            replies = 0
        interested = 0
        closed = 0

        pending_email = 0
        email_remaining_today = 0
        if db:
            pending_email = db.count("leads", active_lead_filters)
            # If queue exists but batch_state is inactive (e.g. older run), auto-activate so dashboard shows cumulative counts.
            try:
                _ensure_batch_state_loaded()
                bs = agent_state.get("batch_state") or {}
                if pending_email > 0 and not bs.get("active"):
                    # Always anchor to current business-day (11:00 local) start so it’s consistent even if started at 12.
                    from modules.database import get_business_day_range
                    _, start_utc, _ = get_business_day_range(reset_hour_local=11)
                    _set_batch_active(start_utc_iso=str(start_utc), lead_target=int(getattr(config, "DAILY_LEAD_TARGET", 120) or 120))
            except Exception:
                pass
            try:
                from modules.database import get_business_day_range
                _, start_utc, end_utc = get_business_day_range(reset_hour_local=11)
                sent_today = int(db.count("outreach_log", {"and": f"(channel.eq.email,sent_at.gte.{start_utc},sent_at.lt.{end_utc})"}) or 0)
                daily_target = int(getattr(config, "DAILY_EMAIL_TARGET", 40) or 40)
                email_remaining_today = max(0, daily_target - sent_today)
            except Exception:
                email_remaining_today = 0

        mrr = 0

        # Source tracker
        sources = db.select("source_tracker", order="last_scraped.desc", limit=100) if db else []

        # Segment performance
        segments = db.select("segment_performance", order="close_rate.desc") if db else []

        return {
            "connected": db is not None,
            "total_leads": pending_email,
            "total_leads_all": total,
            "today": today,
            "daily_reset_notice": agent_state.get("daily_reset_notice"),
            "hot_leads": hot[:10],
            "leads": leads,
            "stats": {
                "emailed": emailed,
                "forms_filled": forms,
                "replies": replies,
                "interested": interested,
                "closed": closed,
                "mrr": mrr,
                "pending_email": pending_email,
                "email_remaining_today": email_remaining_today,
            },
            "sources": sources,
            "segments": segments,
        }
    except Exception as e:
        logger.error(f"Dashboard data error: {e}")
        return {"connected": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# HTTP SERVER
# ═══════════════════════════════════════════════════════════════

class AgentHTTPHandler(SimpleHTTPRequestHandler):
    """Custom HTTP handler for the agent API + dashboard."""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # API Routes
        if path == "/api/status":
            _daily_business_reset_if_needed()
            self._json_response({
                "status": agent_state["status"],
                "current_step": agent_state["current_step"],
                "pipeline_running": agent_state["pipeline_running"],
                "agent_loop_running": agent_state["agent_loop_running"],
                "cycle": agent_state["cycle"],
                "started_at": agent_state["started_at"],
                "last_run": agent_state["last_run"],
                "stats": agent_state["stats"],
                "workers": agent_state.get("worker_status", {}),
                "completed_phases": agent_state.get("completed_phases", []),
                "daily_reset_notice": agent_state.get("daily_reset_notice"),
                "quota": get_cached_quota(),
            })

        elif path == "/api/logs":
            self._json_response({"logs": agent_state["log"][:50]})

        elif path == "/api/dashboard":
            data = get_dashboard_data()
            self._json_response(data)

        elif path == "/api/leads":
            params = parse_qs(parsed.query)
            from modules.database import db
            if db:
                import config
                filters = {}
                show_all = (params.get("show_all", ["0"])[0] == "1")
                if not show_all:
                    min_score = getattr(config, "SCORE_THRESHOLDS", {}).get("min_qualify", 40)
                    filters["sequence_stage"] = "eq.0"
                    filters["email_sent"] = "eq.false"
                    filters["excluded"] = "eq.false"
                    filters["score"] = f"gte.{min_score}"
                if params.get("country"):
                    filters["country"] = f"eq.{params['country'][0]}"
                if params.get("state"):
                    filters["state"] = f"eq.{params['state'][0]}"
                if params.get("city"):
                    filters["city"] = f"eq.{params['city'][0]}"
                if params.get("lead_type"):
                    filters["lead_type"] = f"eq.{params['lead_type'][0]}"
                if params.get("source"):
                    filters["source"] = f"eq.{params['source'][0]}"
                limit = int(params.get("limit", [100])[0])
                offset = int(params.get("offset", [0])[0])
                leads_raw = db.select("leads", filters=filters, order="created_at.desc", limit=max(limit * 3, 1000))
                leads = (leads_raw or [])[offset:offset+limit]
                total = db.count("leads", filters)
                # Adjust total approximately or leave as is (total in DB)
                self._json_response({"leads": leads, "total": total})
            else:
                self._json_response({"leads": [], "total": 0})

        elif path == "/api/leads/state-city":
            from modules.database import db
            if not db:
                self._json_response({"items": []})
                return
            rows = db.select("leads", columns="country,state,city", order="created_at.desc", limit=5000) or []
            items = []
            seen = set()
            for r in rows:
                country = (r.get("country") or "").strip()
                state = (r.get("state") or "").strip()
                city = (r.get("city") or "").strip()
                key = (country, state, city)
                if key in seen:
                    continue
                seen.add(key)
                items.append({"country": country, "state": state, "city": city})
            self._json_response({"items": items})

        elif path == "/api/hot-leads":
            from modules.database import get_hot_leads
            self._json_response({"leads": get_hot_leads()})

        elif path == "/api/scraping-config":
            import config
            self._json_response({
                "leadTarget": config.DAILY_LEAD_TARGET,
                "minScore": config.SCORE_THRESHOLDS.get("min_qualify", 40),
                "keywords": config.SEARCH_KEYWORDS,
                "countries": config.TARGET_COUNTRIES,
            })

        elif path == "/api/sources":
            from modules.database import db
            sources = db.select("source_tracker", order="last_scraped.desc", limit=200) if db else []
            if db and not sources:
                # Fallback: build source summary from leads if source_tracker has no rows
                leads = db.select("leads", columns="source,keyword_used,country,created_at", limit=10000) or []
                grouped = {}
                for row in leads:
                    source = row.get("source") or "unknown"
                    keyword = row.get("keyword_used") or "-"
                    country = row.get("country") or "-"
                    key = (source, keyword, country)
                    if key not in grouped:
                        grouped[key] = {
                            "source": source,
                            "keyword": keyword,
                            "country": country,
                            "total_found": 0,
                            "last_batch_new": 0,
                            "status": "active",
                            "last_scraped": row.get("created_at"),
                        }
                    grouped[key]["total_found"] += 1
                    grouped[key]["last_batch_new"] += 1
                    created_at = row.get("created_at")
                    if created_at and (not grouped[key]["last_scraped"] or created_at > grouped[key]["last_scraped"]):
                        grouped[key]["last_scraped"] = created_at
                sources = sorted(grouped.values(), key=lambda x: x.get("last_scraped") or "", reverse=True)[:200]
            self._json_response({"sources": sources})

        elif path == "/api/segments":
            from modules.database import get_segment_performance
            all_segments = get_segment_performance()  # all types
            self._json_response({"segments": all_segments})

        elif path == "/api/errors":
            self._json_response({"errors": agent_state["errors"][-20:]})

        elif path == "/api/email-stats":
            from modules.database import db
            params = parse_qs(parsed.query)
            date_from = params.get("date_from", [""])[0]
            date_to = params.get("date_to", [""])[0]
            parsed_from = _parse_ymd(date_from) if date_from else None
            parsed_to = _parse_ymd(date_to) if date_to else None
            if date_from and not parsed_from:
                self._json_response({"error": "Invalid date_from. Use YYYY-MM-DD."}, 404)
                return
            if date_to and not parsed_to:
                self._json_response({"error": "Invalid date_to. Use YYYY-MM-DD."}, 404)
                return
            if parsed_from and parsed_to and parsed_from > parsed_to:
                self._json_response({"error": "date_from cannot be greater than date_to."}, 404)
                return
            if db:
                filters = {"channel": "eq.email"}
                if date_from or date_to:
                    filters["and"] = _date_and("sent_at", date_from, date_to)
                logs = db.select(
                    "outreach_log",
                    filters=filters,
                    columns="lead_id,delivery_status,sent_at",
                    limit=10000,
                ) or []
                total_sent = len(logs)
                recorded = sum(1 for l in logs if l.get("delivery_status") == "recorded")
                sent = sum(1 for l in logs if l.get("delivery_status") == "sent")
                failed = sum(1 for l in logs if l.get("delivery_status") == "failed")

                lead_ids = [l.get("lead_id") for l in logs if l.get("lead_id")]
                opened = 0
                replied = 0
                if lead_ids:
                    id_list = ",".join(sorted(set(str(x) for x in lead_ids)))
                    leads = db.select(
                        "leads",
                        columns="id,email_opened,replied",
                        filters={"id": f"in.({id_list})"},
                        limit=10000,
                    ) or []
                    opened = sum(1 for r in leads if r.get("email_opened"))
                    replied = sum(1 for r in leads if r.get("replied"))

                open_rate = round(opened / total_sent * 100, 1) if total_sent else 0
                reply_rate = round(replied / total_sent * 100, 1) if total_sent else 0
                self._json_response({
                    "total_outreach": total_sent,
                    "recorded": recorded,
                    "sent": sent,
                    "failed": failed,
                    "opened": opened,
                    "replied": replied,
                    "open_rate": open_rate,
                    "reply_rate": reply_rate,
                    "date_from": date_from or None,
                    "date_to": date_to or None,
                })
            else:
                self._json_response({"total_outreach": 0})

        elif path == "/api/warmup-status":
            # Email account health — shows stats per sending email account
            import config
            from modules.database import db
            from modules.email_warmup import get_warmup_status
            domains_result = []
            total_remaining = 0

            instantly_key = (getattr(config, "INSTANTLY_API_KEY", "") or "").strip()
            smtp_user = (getattr(config, "SMTP_USER", "") or "").strip()
            smtp_password = (getattr(config, "SMTP_PASSWORD", "") or "").strip()
            using_instantly = bool(instantly_key and "your" not in instantly_key.lower())
            using_smtp = bool((not using_instantly) and smtp_user and smtp_password and "your" not in smtp_password.lower())

            if using_smtp:
                sending_emails = [smtp_user]
            else:
                sending_emails = getattr(config, "SENDING_EMAILS", []) or []
                if not sending_emails:
                    sending_emails = config.SENDING_DOMAINS or []

            warmup = get_warmup_status()
            warmup_by_domain = {w.get("domain"): w for w in (warmup or [])}

            for email_account in sending_emails:
                actual_domain = email_account.split("@")[-1] if "@" in email_account else email_account
                w = warmup_by_domain.get(email_account, {})
                daily_limit = int(w.get("daily_limit", config.EMAILS_PER_DOMAIN) or config.EMAILS_PER_DOMAIN)
                sent_today = int(w.get("emails_sent", 0) or 0)
                remaining_today = int(w.get("remaining", max(0, daily_limit - sent_today)) or 0)
                warmup_day = int(w.get("warmup_day", 1) or 1)
                d_stats = {
                    "domain": email_account,  # Show full email in UI
                    "actual_domain": actual_domain,
                    "emails_sent": sent_today, "daily_limit": daily_limit,
                    "remaining": remaining_today, "warmup_day": warmup_day,
                    "total_sent": 0, "opened": 0, "replied": 0,
                    "open_rate": 0, "reply_rate": 0,
                    "status": "Active",
                }

                if db:
                    try:
                        # Get real stats — match by domain (outreach_log stores domain, not email)
                        logs = db.select("outreach_log",
                            filters={"channel": "eq.email", "sending_domain": f"eq.{actual_domain}"},
                            columns="id,lead_id,delivery_status,sent_at", limit=5000)
                        d_stats["total_sent"] = len(logs)

                        # Open/reply stats from leads
                        leads_data = db.select("leads",
                            columns="id,email_opened,replied",
                            filters={"email_sent": "eq.true", "sending_domain": f"eq.{actual_domain}"},
                            limit=5000)
                        if leads_data:
                            d_stats["opened"] = sum(1 for l in leads_data if l.get("email_opened"))
                            d_stats["replied"] = sum(1 for l in leads_data if l.get("replied"))
                            total = len(leads_data) or 1
                            d_stats["open_rate"] = round(d_stats["opened"] / total * 100, 1)
                            d_stats["reply_rate"] = round(d_stats["replied"] / total * 100, 1)

                        # Status
                        if d_stats["total_sent"] > 0 and d_stats["open_rate"] > 0:
                            d_stats["status"] = "Active"
                        elif d_stats["total_sent"] > 0:
                            d_stats["status"] = "Sending"
                        else:
                            d_stats["status"] = "Ready"
                    except Exception as e:
                        logger.error(f"Email account stats error for {email_account}: {e}")

                total_remaining += d_stats["remaining"]
                domains_result.append(d_stats)

            self._json_response({
                "domains": domains_result,
                "total_remaining": total_remaining,
            })

        elif path == "/api/email-queue":
            from modules.email_queue import get_queue_size
            self._json_response({"queue_size": get_queue_size()})

        elif path == "/api/email-tracking":
            from modules.database import db
            params = parse_qs(parsed.query)
            date_from = params.get("date_from", [""])[0]
            date_to = params.get("date_to", [""])[0]
            parsed_from = _parse_ymd(date_from) if date_from else None
            parsed_to = _parse_ymd(date_to) if date_to else None
            if date_from and not parsed_from:
                self._json_response({"error": "Invalid date_from. Use YYYY-MM-DD."}, 404)
                return
            if date_to and not parsed_to:
                self._json_response({"error": "Invalid date_to. Use YYYY-MM-DD."}, 404)
                return
            if parsed_from and parsed_to and parsed_from > parsed_to:
                self._json_response({"error": "date_from cannot be greater than date_to."}, 404)
                return
            if db:
                try:
                    # If a date range is provided, scope tracking to emails SENT in that range
                    lead_ids_scope = []
                    if date_from or date_to:
                        ol_filters = {"channel": "eq.email", "and": _date_and("sent_at", date_from, date_to)}
                        logs = db.select(
                            "outreach_log",
                            filters=ol_filters,
                            columns="lead_id,sent_at,delivery_status",
                            limit=10000,
                        ) or []
                        lead_ids_scope = [l.get("lead_id") for l in logs if l.get("lead_id")]
                        lead_ids_scope = list(dict.fromkeys(lead_ids_scope))  # preserve order, unique
                        if not lead_ids_scope:
                            self._json_response({"stats": {"total_tracked": 0, "total_opened": 0, "unique_opens": 0, "open_rate": 0}, "all_tracking": []})
                            return

                    if lead_ids_scope:
                        id_list = ",".join(sorted(set(str(x) for x in lead_ids_scope)))
                        all_tracking = db.select(
                            "email_tracking",
                            filters={"lead_id": f"in.({id_list})"},
                            order="created_at.desc",
                            limit=10000,
                        ) or []
                    else:
                        # No range => last N tracking rows (global view)
                        all_tracking = db.select("email_tracking", order="created_at.desc", limit=1000) or []
                    
                    # Fetch corresponding leads to get website_url, replied, replied_at
                    lead_ids = [t['lead_id'] for t in all_tracking if t.get('lead_id')]
                    leads_data = {}
                    if lead_ids:
                        # Batch fetch leads
                        id_list = ",".join([str(id) for id in set(lead_ids)])
                        leads_res = db.select("leads", columns="id,website_url,replied,replied_at", filters={"id": f"in.({id_list})"}, limit=1000)
                        for l in leads_res:
                            leads_data[l['id']] = l

                    # Also update leads.email_opened based on tracking data
                    for t in all_tracking:
                        if t.get('lead_id') and t['lead_id'] in leads_data:
                            l = leads_data[t['lead_id']]
                            t['website_url'] = l.get('website_url')
                            t['replied'] = l.get('replied')
                            t['replied_at'] = l.get('replied_at')
                        
                        if t.get("opened") and t.get("lead_id"):
                            try:
                                db.update("leads", {"email_opened": True}, {"id": f"eq.{t['lead_id']}"})
                            except:
                                pass
                    total_tracked = len(all_tracking)
                    # For dashboard cards, treat "opens" as opened leads count (<= total_tracked).
                    opened_leads = [t for t in all_tracking if t.get("opened") and t.get("lead_id")]
                    unique_open_ids = {t.get("lead_id") for t in opened_leads}
                    total_opened = sum(t.get("open_count") or 1 for t in opened_leads)
                    unique_opens = len(unique_open_ids)
                    stats = {
                        "total_tracked": total_tracked,
                        "total_opened": total_opened,
                        "unique_opens": unique_opens,
                        "open_rate": round((total_opened / total_tracked * 100), 1) if total_tracked else 0,
                    }
                    # Sort: replied first, then opened, then recent
                    def _ts(v):
                        try:
                            return datetime.fromisoformat((v or "").replace("Z", "+00:00")).timestamp()
                        except Exception:
                            return 0
                    all_tracking_sorted = sorted(
                        all_tracking,
                        key=lambda t: (
                            1 if t.get("replied") else 0,
                            1 if t.get("opened") else 0,
                            _ts(t.get("replied_at") or t.get("opened_at") or t.get("created_at")),
                        ),
                        reverse=True,
                    )
                    # Keep payload bounded
                    self._json_response({"stats": stats, "all_tracking": all_tracking_sorted[:200]})
                except Exception as e:
                    self._json_response({"stats": {"total_tracked": 0, "total_opened": 0, "unique_opens": 0, "open_rate": 0}, "all_tracking": [], "error": str(e)})
            else:
                self._json_response({"stats": {"total_tracked": 0, "total_opened": 0, "unique_opens": 0, "open_rate": 0}, "all_tracking": []})

        elif path == "/api/domain-emails":
            from modules.database import db
            params = parse_qs(parsed.query)
            domain = params.get("domain", [""])[0]
            date_from = params.get("date_from", [""])[0]
            date_to = params.get("date_to", [""])[0]
            if db and domain:
                logs = db.select("outreach_log",
                    filters={
                        "channel": "eq.email",
                        "sending_domain": f"eq.{domain}",
                        **({"and": _date_and("sent_at", date_from, date_to)} if (date_from or date_to) else {})
                    },
                    order="sent_at.desc",
                    limit=200)
                # Enrich with lead + full tracking info
                for log_entry in logs:
                    if log_entry.get("lead_id"):
                        lead_rows = db.select("leads",
                            columns="company_name,company_domain,contact_email,contact_name,email_opened,replied,replied_at,score,country,lead_type",
                            filters={"id": f"eq.{log_entry['lead_id']}"},
                            limit=1)
                        if lead_rows:
                            log_entry["lead"] = lead_rows[0]
                        # Get full tracking data including device info
                        tracking_rows = db.select("email_tracking",
                            columns="opened,open_count,opened_at,user_agent,ip_address",
                            filters={"lead_id": f"eq.{log_entry['lead_id']}", "sequence_stage": f"eq.{log_entry.get('sequence_stage',1)}"},
                            limit=1)
                        if tracking_rows:
                            log_entry["tracking"] = tracking_rows[0]
                # Summary stats
                total = len(logs)
                sent_count = sum(1 for l in logs if l.get("delivery_status") in ("sent", "recorded"))
                opened_count = sum(1 for l in logs if l.get("tracking", {}).get("opened"))
                replied_count = sum(1 for l in logs if l.get("lead", {}).get("replied"))
                failed_count = sum(1 for l in logs if l.get("delivery_status") == "failed")
                self._json_response({
                    "emails": logs,
                    "domain": domain,
                    "summary": {
                        "total": total,
                        "sent": sent_count,
                        "opened": opened_count,
                        "replied": replied_count,
                        "failed": failed_count,
                        "open_rate": round(opened_count / sent_count * 100, 1) if sent_count else 0,
                        "reply_rate": round(replied_count / sent_count * 100, 1) if sent_count else 0,
                    }
                })
            else:
                self._json_response({"emails": [], "domain": domain, "summary": {}})

        elif path == "/api/form-outreach/status":
            from modules.form_outreach import get_outreach_status
            self._json_response(get_outreach_status())

        elif path == "/api/form-outreach/counts":
            from modules.database import get_form_outreach_counts
            self._json_response(get_form_outreach_counts())

        elif path == "/api/funnel":
            from modules.database import get_funnel_counts
            self._json_response(get_funnel_counts())

        elif path == "/api/form-outreach/results":
            from modules.database import get_form_outreach_results
            params = parse_qs(parsed.query)
            limit = int(params.get("limit", [100])[0])
            status_filter = params.get("status", [None])[0]
            date_from = params.get("date_from", [None])[0]
            date_to = params.get("date_to", [None])[0]
            search = params.get("search", [None])[0]
            parsed_from = _parse_ymd(date_from or "")
            parsed_to = _parse_ymd(date_to or "")
            if date_from and not parsed_from:
                self._json_response({"error": "Invalid date_from. Use YYYY-MM-DD."}, 404)
                return
            if date_to and not parsed_to:
                self._json_response({"error": "Invalid date_to. Use YYYY-MM-DD."}, 404)
                return
            if parsed_from and parsed_to and parsed_from > parsed_to:
                self._json_response({"error": "date_from cannot be greater than date_to."}, 404)
                return
            results = get_form_outreach_results(
                limit=limit, status=status_filter,
                date_from=date_from, date_to=date_to,
                search=search
            )
            success = sum(1 for r in results if r.get("form_submission_status") == "success")
            failed = sum(1 for r in results if r.get("form_submission_status") == "failed")
            processing = sum(1 for r in results if r.get("form_submission_status") == "processing")
            self._json_response({
                "results": results,
                "total": len(results),
                "counts": {"success": success, "failed": failed, "processing": processing}
            })

        elif path == "/api/email-sequences":
            from modules.database import db
            params = parse_qs(parsed.query)
            stage_filter = params.get("stage", [""])[0]
            date_from = params.get("date_from", [""])[0]
            date_to = params.get("date_to", [""])[0]
            parsed_from = _parse_ymd(date_from) if date_from else None
            parsed_to = _parse_ymd(date_to) if date_to else None
            if date_from and not parsed_from:
                self._json_response({"error": "Invalid date_from. Use YYYY-MM-DD."}, 404)
                return
            if date_to and not parsed_to:
                self._json_response({"error": "Invalid date_to. Use YYYY-MM-DD."}, 404)
                return
            if parsed_from and parsed_to and parsed_from > parsed_to:
                self._json_response({"error": "date_from cannot be greater than date_to."}, 404)
                return
            if db:
                filters = {"channel": "eq.email"}
                if stage_filter:
                    filters["sequence_stage"] = f"eq.{stage_filter}"
                if date_from or date_to:
                    filters["and"] = _date_and("sent_at", date_from, date_to)
                logs = db.select("outreach_log",
                    filters=filters,
                    order="sent_at.desc",
                    limit=200)
                for log_entry in logs:
                    if log_entry.get("lead_id"):
                        lead_rows = db.select("leads",
                            columns="company_name,company_domain,contact_email,email_opened,replied",
                            filters={"id": f"eq.{log_entry['lead_id']}"},
                            limit=1)
                        if lead_rows:
                            log_entry["lead"] = lead_rows[0]
                self._json_response({"sequences": logs})
            else:
                self._json_response({"sequences": []})

        elif path == "/api/chat-analytics":
            # Structured analytics for the chat command center
            from modules.database import db
            analytics = {"funnel": {}, "top_countries": [], "domain_health": [], "recommendations": []}
            if db:
                try:
                    all_leads = db.select("leads", columns="country,lead_type,email_sent,replied,closed,score,source", limit=10000)
                    total = len(all_leads)
                    emailed = sum(1 for l in all_leads if l.get("email_sent"))
                    replied = sum(1 for l in all_leads if l.get("replied"))
                    closed = sum(1 for l in all_leads if l.get("closed"))
                    analytics["funnel"] = {
                        "total": total, "emailed": emailed, "replied": replied, "closed": closed,
                        "reply_rate": round(replied / emailed * 100, 1) if emailed else 0,
                        "close_rate": round(closed / emailed * 100, 1) if emailed else 0,
                    }
                    # Country breakdown
                    cs = {}
                    for l in all_leads:
                        c = l.get("country", "?")
                        if c not in cs: cs[c] = {"total":0,"emailed":0,"replied":0,"closed":0}
                        cs[c]["total"] += 1
                        if l.get("email_sent"): cs[c]["emailed"] += 1
                        if l.get("replied"): cs[c]["replied"] += 1
                        if l.get("closed"): cs[c]["closed"] += 1
                    analytics["top_countries"] = sorted(
                        [{"country":c, **s, "reply_rate": round(s["replied"]/s["emailed"]*100,1) if s["emailed"] else 0}
                         for c,s in cs.items()],
                        key=lambda x: -x["total"]
                    )[:10]
                    # Recommendations
                    recs = []
                    for c, s in cs.items():
                        if s["emailed"] >= 200 and s["closed"] == 0:
                            recs.append(f"Pause {c} — {s['emailed']} emails, 0 closes")
                    if analytics["funnel"]["reply_rate"] < 2:
                        recs.append("Reply rate below 2% — check domain health and email content")
                    analytics["recommendations"] = recs
                except Exception as e:
                    analytics["error"] = str(e)
            self._json_response(analytics)

        elif path == "/api/email-config":
            import config
            # Show the configured sending emails, not the SMTP user
            sending = getattr(config, 'SENDING_EMAILS', config.SENDING_DOMAINS)
            self._json_response({
                "sender_email": ", ".join(sending) if sending else os.getenv("SENDER_EMAIL", ""),
                "sender_name": os.getenv("SMTP_FROM_NAME", ""),
                "smtp_host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
                "smtp_port": os.getenv("SMTP_PORT", "587"),
            })

        elif path.startswith("/api/track-open"):
            # Email open tracking pixel endpoint
            # When email client loads this 1x1 pixel, we record the open
            params = parse_qs(parsed.query)
            tid = params.get("id", [""])[0]
            if tid:
                try:
                    from modules.database import db
                    if db:
                        # Update email_tracking record
                        now = datetime.now(timezone.utc).isoformat()
                        existing = db.select("email_tracking", filters={"tracking_id": f"eq.{tid}"}, limit=1)
                        if existing:
                            track = existing[0]
                            open_count = (track.get("open_count") or 0) + 1
                            db.update("email_tracking", {
                                "opened": True,
                                "opened_at": now,
                                "open_count": open_count,
                                "user_agent": self.headers.get("User-Agent", ""),
                                "ip_address": self.client_address[0],
                            }, {"tracking_id": f"eq.{tid}"})
                            # Also update the lead's email_opened field
                            lead_id = track.get("lead_id")
                            if lead_id:
                                db.update("leads", {"email_opened": True}, {"id": f"eq.{lead_id}"})
                            add_log(f"Email opened: {track.get('recipient_email', '?')} (open #{open_count})", category="email")
                except Exception as e:
                    logger.error(f"Track open error: {e}")
            # Return a 1x1 transparent GIF
            import base64
            pixel = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.send_header("Content-Length", str(len(pixel)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(pixel)

        elif path == "/api/report-data":
            from modules.database import db
            params = parse_qs(parsed.query)
            date_from = params.get("date_from", [""])[0]
            date_to = params.get("date_to", [""])[0]
            parsed_from = _parse_ymd(date_from) if date_from else None
            parsed_to = _parse_ymd(date_to) if date_to else None
            if date_from and not parsed_from:
                self._json_response({"error": "Invalid date_from. Use YYYY-MM-DD."}, 404)
                return
            if date_to and not parsed_to:
                self._json_response({"error": "Invalid date_to. Use YYYY-MM-DD."}, 404)
                return
            if parsed_from and parsed_to and parsed_from > parsed_to:
                self._json_response({"error": "date_from cannot be greater than date_to."}, 404)
                return
            if db:
                try:
                    # Consider up to 10k leads to ensure date filters capture older data
                    all_leads = db.select("leads", order="created_at.desc", limit=10000) or []

                    # Build range-aware lead view so report cards/table use exact selected dates.
                    range_opened_ids = set()
                    if date_from or date_to:
                        try:
                            t_filters = {"opened": "eq.true", "and": _date_and("opened_at", date_from, date_to)}
                            t_rows = db.select("email_tracking", filters=t_filters, columns="lead_id", limit=5000) or []
                            range_opened_ids = {r.get("lead_id") for r in t_rows if r.get("lead_id")}
                        except Exception:
                            range_opened_ids = set()

                    scoped_leads = []
                    for lead in all_leads:
                        email_in = _in_date_range(lead.get("email_sent_at", ""), date_from, date_to) if (date_from or date_to) else bool(lead.get("email_sent"))
                        form_in = _in_date_range(lead.get("form_last_attempted_at", ""), date_from, date_to) if (date_from or date_to) else bool(lead.get("form_filled"))
                        reply_in = _in_date_range(lead.get("replied_at", ""), date_from, date_to) if (date_from or date_to) else bool(lead.get("replied"))
                        created_in = _in_date_range(lead.get("created_at", ""), date_from, date_to) if (date_from or date_to) else True
                        opened_in = (lead.get("id") in range_opened_ids) if (date_from or date_to) else bool(lead.get("email_opened"))

                        if date_from or date_to:
                            if not (created_in or email_in or form_in or reply_in or opened_in):
                                continue

                        row = dict(lead)
                        row["email_sent"] = bool(email_in)
                        row["form_filled"] = bool(form_in)
                        row["replied"] = bool(reply_in)
                        row["email_opened"] = bool(opened_in)
                        scoped_leads.append(row)

                    total = len(scoped_leads)
                    emailed = sum(1 for l in scoped_leads if l.get("email_sent"))
                    opened = sum(1 for l in scoped_leads if l.get("email_opened"))
                    forms_filled = sum(1 for l in scoped_leads if l.get("form_filled"))
                    replies = sum(1 for l in scoped_leads if l.get("replied"))
                    closed = sum(1 for l in scoped_leads if l.get("closed"))
                    outreached = sum(1 for l in scoped_leads if l.get("email_sent") or l.get("form_filled"))
                    total_opens = 0
                    if db:
                        try:
                            # If no date range, we look at total tracked opens in the database
                            tr_filters = {}
                            if date_from or date_to:
                                tr_filters["and"] = _date_and("created_at", date_from, date_to)
                            
                            tracking_res = db.select("email_tracking", filters=tr_filters, columns="open_count") or []
                            total_opens = sum(t.get("open_count") or 0 for t in tracking_res)
                        except Exception as e:
                            logger.error(f"Error summing opens in report: {e}")

                    self._json_response({
                        "total": total,
                        "emailed": emailed,
                        "opened": opened,
                        "total_opens": total_opens,
                        "forms_filled": forms_filled,
                        "replies": replies,
                        "closed": closed,
                        "outreached": outreached,
                        "date_from": date_from or None,
                        "date_to": date_to or None,
                        "leads": scoped_leads[:1000],
                    })
                except Exception as e:
                    self._json_response({"error": str(e)}, 500)
            else:
                self._json_response({"total":0,"emailed":0,"forms_filled":0,"replies":0,"closed":0,"outreached":0,"leads":[]})

        elif path == "/api/reconcile-leads":
            result = _sync_existing_lead_records()
            agent_state["initial_sync_done"] = True
            self._json_response({"status": "ok", **result})

        elif path == "/api/trim-pending-emails":
            params = parse_qs(parsed.query)
            keep = int(params.get("keep", ["40"])[0])
            result = _trim_pending_email_queue(keep=keep)
            # Treat this as "today's active lead target"
            try:
                agent_state["lead_target_override"] = int(keep)
                agent_state["lead_target_override_business_date"] = _current_business_date_str()
            except Exception:
                pass
            # Ensure batch is marked active so dashboard counts start immediately.
            try:
                _ensure_batch_state_loaded()
                bs = agent_state.get("batch_state") or {}
                if not bs.get("active"):
                    from modules.database import get_business_day_range
                    _, start_utc, _ = get_business_day_range(reset_hour_local=11)
                    _set_batch_active(start_utc_iso=start_utc, lead_target=keep)
            except Exception:
                pass
            self._json_response({"status": "ok", **result})

        elif path == "/admin":
            self.path = "/admin-login.html"
            return SimpleHTTPRequestHandler.do_GET(self)

        elif path == "/admin/panel":
            self.path = "/admin-panel.html"
            return SimpleHTTPRequestHandler.do_GET(self)

        elif path == "/api/admin/settings":
            if not _check_admin_token(self):
                self._json_response({"error": "Unauthorized"}, 401)
                return
            import config
            self._json_response({
                "apifyKey": os.getenv("APIFY_API_KEY", ""),
                "openRouterKey": os.getenv("OPENROUTER_API_KEY", ""),
                "apolloKey": os.getenv("APOLLO_API_KEY", ""),
                "anthropicKey": os.getenv("ANTHROPIC_API_KEY", ""),
                "instantlyKey": os.getenv("INSTANTLY_API_KEY", ""),
                "instantlyWorkspace": os.getenv("INSTANTLY_WORKSPACE_ID", ""),
                "senderEmail": os.getenv("SENDER_EMAIL", ""),
                "senderName": os.getenv("SMTP_FROM_NAME", ""),
                "smtpHost": os.getenv("SMTP_HOST", "smtp.gmail.com"),
                "smtpPort": os.getenv("SMTP_PORT", "587"),
                "smtpUser": os.getenv("SMTP_USER", ""),
                "smtpPassword": os.getenv("SMTP_PASSWORD", ""),
                "notificationEmail": os.getenv("NOTIFICATION_EMAIL", ""),
                "emailSubject": getattr(config, 'EMAIL_SUBJECT', os.getenv("EMAIL_SUBJECT", "CNC Turning Job Work Partnership — Microdyne Engineering")),
                "emailBody": getattr(config, 'EMAIL_BODY', os.getenv("EMAIL_BODY", config.EMAIL_BODY)),
                "supabaseUrl": os.getenv("SUPABASE_URL", ""),
                "supabaseKey": os.getenv("SUPABASE_KEY", ""),
                "leadTarget": config.DAILY_LEAD_TARGET,
                "minScore": config.SCORE_THRESHOLDS.get("min_qualify", 40),
                "keywords": ", ".join(_extra_keywords(config.SEARCH_KEYWORDS)),
                "bestKeywords": list(config.BEST_SEARCH_KEYWORDS),
                "dailyEmailLimit": config.DAILY_EMAIL_LIMIT,
                "dailyFormLimit": config.DAILY_FORM_LIMIT,
                "countries": ", ".join(config.TARGET_COUNTRIES),
                "emailsPerDomain": config.EMAILS_PER_DOMAIN,
                "pauseCountry": config.AUTO_EXCLUSION.get("country_pause_after_leads", 200),
                "minClose": config.AUTO_EXCLUSION.get("lead_type_min_close_rate", 0.005) * 100,
                "minReply": config.AUTO_EXCLUSION.get("source_min_reply_rate", 0.01) * 100,
                "domains": "\n".join(getattr(config, 'SENDING_EMAILS', config.SENDING_DOMAINS)),
                "formName": config.FORM_FILL_DATA.get("name", ""),
                "formEmail": config.FORM_FILL_DATA.get("email", ""),
                "formPhone": config.FORM_FILL_DATA.get("phone", ""),
                "formSubject": config.FORM_FILL_DATA.get("subject", ""),
                "formMessage": config.FORM_FILL_DATA.get("message", ""),
                "formReplyEmail": os.getenv("FORM_REPLY_IMAP_USER", ""),
                "formReplyPassword": os.getenv("FORM_REPLY_IMAP_PASSWORD", ""),
                "adminUser": ADMIN_CREDENTIALS["username"],
                "allowDefaultLogin": ALLOW_DEFAULT_LOGIN,
                "defaultAdminUser": DEFAULT_ADMIN_CREDENTIALS["username"],
            })

        elif path == "/" or path == "/dashboard":
            # Serve the dashboard
            self.path = "/dashboard.html"
            return SimpleHTTPRequestHandler.do_GET(self)

        else:
            # Serve static files (dashboard.html, etc.)
            return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        global ALLOW_DEFAULT_LOGIN
        parsed = urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length > 0 else {}

        if path == "/api/admin/login":
            u = body.get("username", "")
            p = body.get("password", "")
            valid = (u == ADMIN_CREDENTIALS["username"] and p == ADMIN_CREDENTIALS["password"])
            used_default = False
            if not valid and ALLOW_DEFAULT_LOGIN:
                valid = (u == DEFAULT_ADMIN_CREDENTIALS["username"] and p == DEFAULT_ADMIN_CREDENTIALS["password"])
                used_default = valid
            if valid:
                token = secrets.token_hex(32)
                _admin_tokens.add(token)
                add_log(f"Admin login successful{' (default credentials)' if used_default else ''}", category="system")
                self._json_response({"token": token})
            else:
                self._json_response({"error": "Invalid username or password"}, 401)
            return

        elif path == "/api/admin/save":
            if not _check_admin_token(self):
                self._json_response({"error": "Unauthorized"}, 401)
                return
            try:
                import config, re
                section = body.get("section", "")
                env_path = os.path.join(os.path.dirname(__file__), ".env")
                env_content = ""
                if os.path.exists(env_path):
                    with open(env_path, "r") as f:
                        env_content = f.read()

                def _set_env(var, val):
                    nonlocal env_content
                    safe_val = str(val).replace("\r", "").replace("\n", "\\n")
                    os.environ[var] = safe_val
                    if f"{var}=" in env_content:
                        env_content = re.sub(f"{var}=.*", f"{var}={safe_val}", env_content)
                    else:
                        env_content += f"\n{var}={safe_val}"

                if section == "apiKeys":
                    if body.get("apifyKey"): _set_env("APIFY_API_KEY", body["apifyKey"]); config.APIFY_API_KEY = body["apifyKey"]
                    if body.get("openRouterKey"): _set_env("OPENROUTER_API_KEY", body["openRouterKey"]); config.OPENROUTER_API_KEY = body["openRouterKey"]
                    if body.get("apolloKey"): _set_env("APOLLO_API_KEY", body["apolloKey"]); config.APOLLO_API_KEY = body["apolloKey"]
                    if body.get("anthropicKey"): _set_env("ANTHROPIC_API_KEY", body["anthropicKey"]); config.ANTHROPIC_API_KEY = body["anthropicKey"]
                    if body.get("instantlyKey"): _set_env("INSTANTLY_API_KEY", body["instantlyKey"]); config.INSTANTLY_API_KEY = body["instantlyKey"]
                    if body.get("instantlyWorkspace"): _set_env("INSTANTLY_WORKSPACE_ID", body["instantlyWorkspace"]); config.INSTANTLY_WORKSPACE_ID = body["instantlyWorkspace"]

                elif section == "email":
                    if body.get("senderEmail"): _set_env("SENDER_EMAIL", body["senderEmail"]); config.SENDER_EMAIL = body["senderEmail"]
                    if body.get("senderName"): _set_env("SMTP_FROM_NAME", body["senderName"]); config.SMTP_FROM_NAME = body["senderName"]
                    if body.get("smtpHost"): _set_env("SMTP_HOST", body["smtpHost"]); config.SMTP_HOST = body["smtpHost"]
                    if body.get("smtpPort"): _set_env("SMTP_PORT", body["smtpPort"]); config.SMTP_PORT = int(body["smtpPort"])
                    if body.get("smtpUser"): _set_env("SMTP_USER", body["smtpUser"]); config.SMTP_USER = body["smtpUser"]
                    if body.get("smtpPassword"): _set_env("SMTP_PASSWORD", body["smtpPassword"]); config.SMTP_PASSWORD = body["smtpPassword"]
                    if body.get("notificationEmail"): _set_env("NOTIFICATION_EMAIL", body["notificationEmail"]); config.NOTIFICATION_EMAIL = body["notificationEmail"]
                    if "emailSubject" in body:
                        config.EMAIL_SUBJECT = body["emailSubject"]
                        _set_env("EMAIL_SUBJECT", body["emailSubject"])
                    if "emailBody" in body:
                        config.EMAIL_BODY = body["emailBody"]
                        encoded = base64.b64encode((body["emailBody"] or "").encode("utf-8")).decode("ascii")
                        _set_env("EMAIL_BODY_B64", encoded)

                elif section == "supabase":
                    if body.get("supabaseUrl"): _set_env("SUPABASE_URL", body["supabaseUrl"]); config.SUPABASE_URL = body["supabaseUrl"]
                    if body.get("supabaseKey"): _set_env("SUPABASE_KEY", body["supabaseKey"]); config.SUPABASE_KEY = body["supabaseKey"]
                    # Reconnect DB
                    from modules import database as dbmod
                    dbmod.db = dbmod.SupabaseREST(body.get("supabaseUrl", config.SUPABASE_URL), body.get("supabaseKey", config.SUPABASE_KEY))

                elif section == "scraping":
                    if body.get("leadTarget"):
                        config.DAILY_LEAD_TARGET = int(body["leadTarget"])
                        _set_env("DAILY_LEAD_TARGET", config.DAILY_LEAD_TARGET)
                    if body.get("minScore"):
                        config.SCORE_THRESHOLDS["min_qualify"] = int(body["minScore"])
                        _set_env("MIN_QUALIFY_SCORE", config.SCORE_THRESHOLDS["min_qualify"])
                    if "keywords" in body:
                        extras = _extra_keywords(body["keywords"])
                        config.SEARCH_KEYWORDS = config.merge_keywords(extras)
                        _set_env("SEARCH_KEYWORDS", json.dumps(extras))
                    if body.get("countries"):
                        if isinstance(body["countries"], list):
                            config.TARGET_COUNTRIES = [c.strip() for c in body["countries"] if c.strip()]
                        elif isinstance(body["countries"], str):
                            config.TARGET_COUNTRIES = [c.strip() for c in body["countries"].split(",") if c.strip()]
                        _set_env("TARGET_COUNTRIES", json.dumps(config.TARGET_COUNTRIES))

                elif section == "emailConfig":
                    if body.get("emailsPerDomain"): config.EMAILS_PER_DOMAIN = int(body["emailsPerDomain"])
                    if body.get("domains"):
                        raw_entries = [d.strip() for d in body["domains"].split("\n") if d.strip()]
                        # Store full email accounts
                        config.SENDING_EMAILS = raw_entries
                        try:
                            encoded = base64.b64encode(("\n".join(raw_entries)).encode("utf-8")).decode("ascii")
                            _set_env("SENDING_EMAILS_B64", encoded)
                        except Exception:
                            pass
                        # Extract unique domains
                        clean_domains = []
                        for d in raw_entries:
                            domain = d.split("@")[-1] if "@" in d else d
                            if domain and domain not in clean_domains:
                                clean_domains.append(domain)
                        config.SENDING_DOMAINS = clean_domains
                        add_log(f"Sending emails updated: {', '.join(raw_entries)}", category="system")
                    if body.get("smtpAccountsJson"):
                        try:
                            raw = body.get("smtpAccountsJson") or ""
                            encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
                            _set_env("SMTP_ACCOUNTS_B64", encoded)
                        except Exception:
                            pass
                    if body.get("pauseCountry"): config.AUTO_EXCLUSION["country_pause_after_leads"] = int(body["pauseCountry"])
                    if body.get("minClose"): config.AUTO_EXCLUSION["lead_type_min_close_rate"] = float(body["minClose"]) / 100
                    if body.get("minReply"): config.AUTO_EXCLUSION["source_min_reply_rate"] = float(body["minReply"]) / 100

                elif section == "formContent":
                    if body.get("formName"):
                        config.FORM_FILL_DATA["name"] = body["formName"]
                        config.FORM_FILL_DATA["company"] = body["formName"]
                        _set_env("FORM_NAME", body["formName"])
                        _set_env("FORM_COMPANY", body["formName"])
                        # Split name for first/last
                        parts = body["formName"].split(" ", 1)
                        config.FORM_FILL_DATA["first_name"] = parts[0]
                        config.FORM_FILL_DATA["last_name"] = parts[1] if len(parts) > 1 else parts[0]
                        _set_env("FORM_FIRST_NAME", config.FORM_FILL_DATA["first_name"])
                        _set_env("FORM_LAST_NAME", config.FORM_FILL_DATA["last_name"])
                    if body.get("formEmail"):
                        config.FORM_FILL_DATA["email"] = body["formEmail"]
                        _set_env("FORM_EMAIL", body["formEmail"])
                    if body.get("formPhone"):
                        config.FORM_FILL_DATA["phone"] = body["formPhone"]
                        _set_env("FORM_PHONE", body["formPhone"])
                    if body.get("formSubject"):
                        config.FORM_FILL_DATA["subject"] = body["formSubject"]
                        _set_env("FORM_SUBJECT", body["formSubject"])
                    if body.get("formMessage"):
                        config.FORM_FILL_DATA["message"] = body["formMessage"]
                        _set_env("FORM_MESSAGE", body["formMessage"])
                    add_log(f"Form content updated: {config.FORM_FILL_DATA.get('name','')} | {config.FORM_FILL_DATA.get('email','')}", category="system")

                elif section == "formReply":
                    if body.get("formReplyEmail"):
                        _set_env("FORM_REPLY_IMAP_USER", body["formReplyEmail"])
                    if body.get("formReplyPassword"):
                        _set_env("FORM_REPLY_IMAP_PASSWORD", body["formReplyPassword"])
                    add_log("Form reply IMAP credentials updated", category="system")

                elif section == "adminCreds":
                    if body.get("adminUser"):
                        ADMIN_CREDENTIALS["username"] = body["adminUser"]
                        _set_env("ADMIN_USERNAME", body["adminUser"])
                    if body.get("adminPass"):
                        ADMIN_CREDENTIALS["password"] = body["adminPass"]
                        _set_env("ADMIN_PASSWORD", body["adminPass"])
                    if "allowDefaultLogin" in body:
                        ALLOW_DEFAULT_LOGIN = bool(body["allowDefaultLogin"])
                        _set_env("ALLOW_DEFAULT_LOGIN", "true" if ALLOW_DEFAULT_LOGIN else "false")

                env_lines = []
                for line in env_content.splitlines():
                    s = line.strip()
                    if not s or s.startswith("#"):
                        env_lines.append(line)
                        continue
                    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line):
                        env_lines.append(line)
                env_content = "\n".join(env_lines).strip() + "\n"

                # Write .env file
                with open(env_path, "w") as f:
                    f.write(env_content)

                add_log(f"Admin: {section} settings saved", category="system")
                self._json_response({"status": "saved", "section": section})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        elif path == "/api/run-step":
            step = body.get("step", "")
            if agent_state["status"] == "running":
                self._json_response({"error": "Agent is already running"}, 409)
                return
            if step not in ["scrape", "qualify", "store", "email", "forms", "followup", "report"]:
                self._json_response({"error": f"Invalid step: {step}"}, 400)
                return
            t = threading.Thread(target=run_step_thread, args=(step,), daemon=True)
            t.start()
            self._json_response({"status": "started", "step": step})

        elif path == "/api/run-pipeline":
            if agent_state["pipeline_running"] or not start_daily_cycle(trigger="full pipeline"):
                self._json_response({"error": "Pipeline already running"}, 409)
                return
            self._json_response({"status": "pipeline_started"})

        elif path == "/api/start-agent":
            if not start_daily_cycle(trigger="started from dashboard"):
                self._json_response({"error": "Agent already running"}, 409)
                return
            self._json_response({"status": "agent_started", "mode": "daily_cycle"})

        elif path == "/api/stop-agent":
            # Stop ALL workers immediately
            from modules.email_queue import stop_email_workers
            from modules.form_outreach import stop_form_outreach
            stop_email_workers()
            stop_form_outreach()
            agent_state["agent_loop_running"] = False
            agent_state["pipeline_running"] = False
            agent_state["status"] = "idle"
            add_log("Agent stopped — all workers halted", category="system")
            self._json_response({"status": "agent_stopped"})

        elif path == "/api/scraping-config":
            # Update scraping config at runtime
            try:
                import config, re
                env_path = os.path.join(os.path.dirname(__file__), ".env")
                env_content = ""
                if os.path.exists(env_path):
                    with open(env_path, "r", encoding="utf-8") as f:
                        env_content = f.read()

                def _set_env(var, val):
                    nonlocal env_content
                    safe_val = str(val).replace("\r", "").replace("\n", "\\n")
                    os.environ[var] = safe_val
                    if f"{var}=" in env_content:
                        env_content = re.sub(f"{var}=.*", f"{var}={safe_val}", env_content)
                    else:
                        env_content += f"\n{var}={safe_val}"

                if body.get("leadTarget"):
                    config.DAILY_LEAD_TARGET = int(body["leadTarget"])
                    _set_env("DAILY_LEAD_TARGET", config.DAILY_LEAD_TARGET)
                if body.get("minScore"):
                    config.SCORE_THRESHOLDS["min_qualify"] = int(body["minScore"])
                    _set_env("MIN_QUALIFY_SCORE", config.SCORE_THRESHOLDS["min_qualify"])
                if "keywords" in body:
                    extras = _extra_keywords(body["keywords"])
                    config.SEARCH_KEYWORDS = config.merge_keywords(extras)
                    _set_env("SEARCH_KEYWORDS", json.dumps(extras))
                if body.get("countries"):
                    if isinstance(body["countries"], list):
                        config.TARGET_COUNTRIES = body["countries"]
                    elif isinstance(body["countries"], str):
                        config.TARGET_COUNTRIES = [c.strip() for c in body["countries"].split(",") if c.strip()]
                    _set_env("TARGET_COUNTRIES", json.dumps(config.TARGET_COUNTRIES))

                with open(env_path, "w", encoding="utf-8") as f:
                    f.write((env_content or "").strip() + "\n")

                add_log(f"Scraping config updated: target={config.DAILY_LEAD_TARGET}, minScore={config.SCORE_THRESHOLDS['min_qualify']}, keywords={len(config.SEARCH_KEYWORDS)}, countries={len(config.TARGET_COUNTRIES)}", category="system")
                self._json_response({"status": "saved"})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/mark-replied":
            # Manually mark a lead as replied
            lead_id = body.get("lead_id", "")
            if not lead_id:
                self._json_response({"error": "Missing lead_id"}, 400)
                return
            try:
                from modules.database import db
                if db:
                    now = datetime.now(timezone.utc).isoformat()
                    result = db.update("leads", {"replied": True, "replied_at": now}, {"id": f"eq.{lead_id}"})
                    if result:
                        add_log(f"Marked lead {lead_id} as replied", category="email")
                        self._json_response({"success": True, "lead_id": lead_id, "replied_at": now})
                    else:
                        self._json_response({"success": False, "error": "Lead not found"}, 404)
                else:
                    self._json_response({"error": "No database connection"}, 500)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/check-replies":
            # Manually trigger reply check right now
            try:
                from modules.reply_tracker import check_replies
                count = check_replies()
                # Also sync email_opened from tracking table
                from modules.database import db
                if db:
                    opened_tracks = db.select("email_tracking", filters={"opened": "eq.true"}, columns="lead_id", limit=500)
                    for t in opened_tracks:
                        if t.get("lead_id"):
                            db.update("leads", {"email_opened": True}, {"id": f"eq.{t['lead_id']}"})
                add_log(f"Manual reply check: {count} new replies found", category="email")
                self._json_response({"replies_found": count})
            except Exception as e:
                add_log(f"Reply check error: {e}", "error", category="email")
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/form-outreach/start":
            from modules.form_outreach import start_form_outreach_background
            batch_size = body.get("batch_size", 10)
            result = start_form_outreach_background(batch_size=int(batch_size))
            add_log(f"▶ Form outreach started (batch={batch_size})", category="form")
            self._json_response(result)

        elif path == "/api/form-outreach/stop":
            from modules.form_outreach import stop_form_outreach
            result = stop_form_outreach()
            add_log("Form outreach stop requested", category="form")
            self._json_response(result)

        elif path == "/api/form-outreach/restart":
            from modules.form_outreach import restart_form_outreach_background
            result = restart_form_outreach_background()
            add_log("Restart: retrying " + str(result.get("processing_count", 0)) + " stuck forms", category="form")
            self._json_response(result)

        elif path == "/api/form-outreach/clean-urls":
            from modules.form_outreach import clean_all_pending_urls
            result = clean_all_pending_urls()
            add_log(f"Cleaned {result.get('cleaned', 0)}/{result.get('total', 0)} URLs", category="form")
            self._json_response(result)

        elif path == "/api/chat":
            # AI chat with the agent
            message = body.get("message", "").strip()
            if not message:
                self._json_response({"error": "Empty message"}, 400)
                return
            try:
                reply = handle_chat(message)
                self._json_response({"reply": reply})
            except Exception as e:
                logger.error(f"Chat error: {e}")
                self._json_response({"reply": f"Sorry, I encountered an error: {str(e)}"})

        elif path == "/api/stop":
            from modules.email_queue import stop_email_workers
            from modules.reply_tracker import stop_reply_tracker
            stop_email_workers()
            stop_reply_tracker()
            agent_state["agent_loop_running"] = False
            agent_state["pipeline_running"] = False
            agent_state["status"] = "idle"
            add_log("All workers stopped", category="system")
            self._json_response({"status": "stop_requested"})

        elif path == "/api/connect":
            # Update Supabase connection
            url = body.get("supabase_url", "")
            key = body.get("supabase_key", "")
            if url and key:
                from modules import database as dbmod
                dbmod.db = dbmod.SupabaseREST(url, key)
                # Update config
                os.environ["SUPABASE_URL"] = url
                os.environ["SUPABASE_KEY"] = key
                # Test connection
                try:
                    test = dbmod.db.count("leads")
                    add_log(f"✓ Connected to Supabase ({test} leads found)")
                    self._json_response({"status": "connected", "leads_count": test})
                except Exception as e:
                    self._json_response({"error": str(e)}, 500)
            else:
                self._json_response({"error": "Missing url or key"}, 400)

        elif path == "/api/update-lead":
            from modules.database import update_lead
            lead_id = body.get("id")
            updates = body.get("updates", {})
            if lead_id and updates:
                result = update_lead(lead_id, updates)
                self._json_response({"status": "updated", "lead": result})
            else:
                self._json_response({"error": "Missing id or updates"}, 400)

        elif path == "/api/settings":
            # Save settings to config at runtime
            try:
                import config
                if body.get("leadTarget"):
                    config.DAILY_LEAD_TARGET = int(body["leadTarget"])
                if body.get("minScore"):
                    config.SCORE_THRESHOLDS["min_qualify"] = int(body["minScore"])
                if body.get("emailsPerDomain"):
                    config.EMAILS_PER_DOMAIN = int(body["emailsPerDomain"])
                if body.get("countries"):
                    config.TARGET_COUNTRIES = [c.strip() for c in body["countries"].split(",") if c.strip()]
                if body.get("domains"):
                    config.SENDING_DOMAINS = [d.strip() for d in body["domains"].split("\n") if d.strip()]
                if body.get("pauseCountry"):
                    config.AUTO_EXCLUSION["country_pause_after_leads"] = int(body["pauseCountry"])
                if body.get("minClose"):
                    config.AUTO_EXCLUSION["lead_type_min_close_rate"] = float(body["minClose"]) / 100
                if body.get("minReply"):
                    config.AUTO_EXCLUSION["source_min_reply_rate"] = float(body["minReply"]) / 100
                if body.get("keywords") and isinstance(body["keywords"], list):
                    config.SEARCH_KEYWORDS = config.merge_keywords(_extra_keywords(body["keywords"]))
                    add_log(f"Search keywords updated: {len(config.SEARCH_KEYWORDS)} keywords", category="system")
                add_log("Settings updated via dashboard", category="system")
                self._json_response({"status": "saved"})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/save-api-keys":
            # Save API keys to runtime config and .env file
            try:
                import config
                updated = []

                if body.get("apifyKey"):
                    config.APIFY_API_KEY = body["apifyKey"]
                    os.environ["APIFY_API_KEY"] = body["apifyKey"]
                    updated.append("Apify")

                if body.get("openRouterKey"):
                    config.OPENROUTER_API_KEY = body["openRouterKey"]
                    os.environ["OPENROUTER_API_KEY"] = body["openRouterKey"]
                    updated.append("OpenRouter")

                if body.get("apolloKey"):
                    config.APOLLO_API_KEY = body["apolloKey"]
                    os.environ["APOLLO_API_KEY"] = body["apolloKey"]
                    updated.append("Apollo")

                if body.get("anthropicKey"):
                    config.ANTHROPIC_API_KEY = body["anthropicKey"]
                    os.environ["ANTHROPIC_API_KEY"] = body["anthropicKey"]
                    updated.append("Anthropic")

                if body.get("instantlyKey"):
                    config.INSTANTLY_API_KEY = body["instantlyKey"]
                    os.environ["INSTANTLY_API_KEY"] = body["instantlyKey"]
                    updated.append("Instantly")

                if body.get("senderEmail"):
                    config.SENDER_EMAIL = body["senderEmail"]
                    config.NOTIFICATION_EMAIL = body["senderEmail"]
                    os.environ["SENDER_EMAIL"] = body["senderEmail"]
                    os.environ["NOTIFICATION_EMAIL"] = body["senderEmail"]
                    updated.append("Email")

                # Also write to .env file so keys persist across restarts
                env_path = os.path.join(os.path.dirname(__file__), ".env")
                if os.path.exists(env_path):
                    with open(env_path, "r") as f:
                        env_content = f.read()
                    env_map = {
                        "apifyKey": ("APIFY_API_KEY", body.get("apifyKey", "")),
                        "openRouterKey": ("OPENROUTER_API_KEY", body.get("openRouterKey", "")),
                        "apolloKey": ("APOLLO_API_KEY", body.get("apolloKey", "")),
                        "anthropicKey": ("ANTHROPIC_API_KEY", body.get("anthropicKey", "")),
                        "instantlyKey": ("INSTANTLY_API_KEY", body.get("instantlyKey", "")),
                        "senderEmail": ("SENDER_EMAIL", body.get("senderEmail", "")),
                    }
                    for key, (env_var, val) in env_map.items():
                        if val:
                            if f"{env_var}=" in env_content:
                                import re
                                env_content = re.sub(f"{env_var}=.*", f"{env_var}={val}", env_content)
                            else:
                                env_content += f"\n{env_var}={val}"
                    with open(env_path, "w") as f:
                        f.write(env_content)

                add_log(f"API keys updated: {', '.join(updated)}", category="system")
                self._json_response({"status": "saved", "updated": updated})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/save-form-message":
            # Parse the single textarea and update FORM_FILL_DATA + FORM_MESSAGE_TEMPLATE
            try:
                import config
                raw = body.get("formMessage", "")
                lines = raw.strip().split("\n")
                name = ""
                phone = ""
                email = ""
                subject = ""
                message_lines = []
                in_message = False

                for line in lines:
                    line_s = line.strip()
                    if line_s.lower().startswith("name:"):
                        name = line_s.split(":", 1)[1].strip()
                    elif line_s.lower().startswith("contact:"):
                        phone = line_s.split(":", 1)[1].strip()
                    elif line_s.lower().startswith("email:"):
                        email = line_s.split(":", 1)[1].strip()
                    elif line_s.lower().startswith("subject:"):
                        subject = line_s.split(":", 1)[1].strip()
                        in_message = True
                    elif in_message or (not line_s.lower().startswith(("name:", "contact:", "email:", "subject:")) and line_s):
                        in_message = True
                        message_lines.append(line_s)

                message = " ".join(message_lines).strip()

                config.FORM_FILL_DATA["name"] = name or config.FORM_FILL_DATA["name"]
                config.FORM_FILL_DATA["company"] = name or config.FORM_FILL_DATA["company"]
                config.FORM_FILL_DATA["phone"] = phone or config.FORM_FILL_DATA["phone"]
                config.FORM_FILL_DATA["email"] = email or config.FORM_FILL_DATA["email"]
                config.FORM_FILL_DATA["subject"] = subject or config.FORM_FILL_DATA["subject"]
                config.FORM_FILL_DATA["message"] = message or config.FORM_FILL_DATA["message"]

                add_log(f"Form content updated: name={name}, subject={subject[:40]}...", category="system")
                self._json_response({"status": "saved", "name": name, "subject": subject})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/export-csv":
            from modules.database import db
            if db:
                leads = db.select("leads", order="created_at.desc", limit=10000)
                self._json_response({"leads": leads})
            else:
                self._json_response({"leads": []})

        else:
            self._json_response({"error": "Not found"}, 404)

    def _json_response(self, data, status=200):
        """Send a JSON response."""
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            payload = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8", errors="replace")
            self.wfile.write(payload)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        """Suppress default access logs (we use our own logging)."""
        pass


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    PORT = int(os.getenv("PORT", 8000))

    # Register structured log callback for modules
    from modules.events import set_log_callback
    set_log_callback(add_log)

    print()
    print("  MicrodyneHunter v2 - Command Center")
    print(f"  Dashboard:  http://localhost:{PORT}")
    print(f"  API:        http://localhost:{PORT}/api/status")
    print()
    print("  The dashboard controls the agent.")
    print("  Press Ctrl+C to stop the server.")
    print()

    # Start reply tracker in background so email replies are detected automatically
    try:
        from modules.reply_tracker import start_reply_tracker, stop_reply_tracker
        start_reply_tracker()
        logger.info("[Server] Reply tracker started at server boot")
    except Exception as e:
        logger.error(f"[Server] Failed to start reply tracker: {e}")

    import config
    if config.AUTO_DAILY_RUN:
        threading.Thread(target=daily_scheduler_thread, daemon=True, name="daily-scheduler").start()
        print(f"  Daily run:  every day after {config.DAILY_RUN_HOUR:02d}:{config.DAILY_RUN_MINUTE:02d} "
              f"(max {config.DAILY_EMAIL_LIMIT} emails, {config.DAILY_FORM_LIMIT} forms)")
        print()

    # Open browser automatically (skip if NOBROWSER env is set)
    if not os.getenv("NOBROWSER"):
        webbrowser.open(f"http://localhost:{PORT}")

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedHTTPServer(("0.0.0.0", PORT), AgentHTTPHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.shutdown()
    finally:
        try:
            from modules.reply_tracker import stop_reply_tracker
            stop_reply_tracker()
        except Exception:
            pass


if __name__ == "__main__":
    main()
