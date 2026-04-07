"""
MicrodyneHunter v2 — Form Outreach Orchestrator
Manual-only: Start Batch for new leads, Restart for stuck processing forms.
Stop halts either operation. No automatic retry.
"""

import logging
import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional

from modules.database import (
    get_leads_for_form_outreach,
    update_form_status,
    get_form_outreach_results,
    update_lead,
    db,
)
from modules.form_filler import run_form_filling, clean_website_url

logger = logging.getLogger("microdynehunter.form_outreach")


# ═══════════════════════════════════════════════════════════════
# OUTREACH STATE (polled by dashboard)
# ═══════════════════════════════════════════════════════════════

outreach_state = {
    "running": False,
    "stop_requested": False,
    "restarting": False,
    "batch_size": 10,
    "total_processed": 0,
    "success": 0,
    "failed": 0,
    "no_form": 0,
    "current_lead": None,
    "started_at": None,
    "finished_at": None,
    "results": [],
    "error": None,
    "urls_cleaned": 0,
}

_worker_thread: Optional[threading.Thread] = None


def _reset_state():
    """Reset counters for a new run."""
    outreach_state["stop_requested"] = False
    outreach_state["total_processed"] = 0
    outreach_state["success"] = 0
    outreach_state["failed"] = 0
    outreach_state["no_form"] = 0
    outreach_state["current_lead"] = None
    outreach_state["started_at"] = datetime.now(timezone.utc).isoformat()
    outreach_state["finished_at"] = None
    outreach_state["results"] = []
    outreach_state["error"] = None
    outreach_state["urls_cleaned"] = 0


def _finalize():
    """Mark run as complete."""
    outreach_state["finished_at"] = datetime.now(timezone.utc).isoformat()
    outreach_state["running"] = False
    outreach_state["restarting"] = False
    outreach_state["current_lead"] = None


def get_outreach_status() -> dict:
    """Return current outreach state for dashboard polling."""
    return {
        "running": outreach_state["running"],
        "restarting": outreach_state["restarting"],
        "batch_size": outreach_state["batch_size"],
        "total_processed": outreach_state["total_processed"],
        "success": outreach_state["success"],
        "failed": outreach_state["failed"],
        "no_form": outreach_state["no_form"],
        "current_lead": outreach_state["current_lead"],
        "started_at": outreach_state["started_at"],
        "finished_at": outreach_state["finished_at"],
        "error": outreach_state["error"],
        "urls_cleaned": outreach_state["urls_cleaned"],
    }


# ═══════════════════════════════════════════════════════════════
# URL CLEANING
# ═══════════════════════════════════════════════════════════════

def clean_lead_urls(leads: list[dict]) -> int:
    cleaned_count = 0
    for lead in leads:
        raw_url = lead.get("website_url", "")
        if not raw_url:
            continue
        cleaned = clean_website_url(raw_url)
        if cleaned and cleaned != raw_url:
            lead["website_url"] = cleaned
            try:
                update_lead(lead["id"], {"website_url": cleaned})
            except Exception as e:
                logger.error(f"Failed to update URL in DB for {lead.get('id')}: {e}")
            cleaned_count += 1
    return cleaned_count


def clean_all_pending_urls() -> dict:
    if not db:
        return {"cleaned": 0, "total": 0, "error": "No database connection"}
    try:
        leads = db.select("leads", filters={
            "form_submission_status": "eq.pending",
        }, order="created_at.asc", limit=5000)
        if not leads:
            return {"cleaned": 0, "total": 0}
        cleaned = 0
        for lead in leads:
            raw_url = lead.get("website_url", "")
            if not raw_url:
                continue
            new_url = clean_website_url(raw_url)
            if new_url and new_url != raw_url:
                try:
                    update_lead(lead["id"], {"website_url": new_url})
                    cleaned += 1
                except Exception:
                    pass
        return {"cleaned": cleaned, "total": len(leads)}
    except Exception as e:
        return {"cleaned": 0, "total": 0, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# START BATCH — Process new pending leads
# ═══════════════════════════════════════════════════════════════

async def run_form_outreach_async(batch_size: int = 10) -> dict:
    """Process new pending leads. Manual only — no auto-retry."""
    outreach_state["running"] = True
    outreach_state["restarting"] = False
    outreach_state["batch_size"] = batch_size
    _reset_state()

    try:
        leads = get_leads_for_form_outreach(limit=batch_size)
        if not leads:
            _finalize()
            return {"total": 0, "message": "No pending leads"}

        logger.info(f"Start Batch: {len(leads)} leads")

        # Clean URLs
        urls_cleaned = clean_lead_urls(leads)
        outreach_state["urls_cleaned"] = urls_cleaned

        # Mark as processing
        for lead in leads:
            if outreach_state["stop_requested"]:
                break
            update_form_status(lead["id"], "processing")

        # Fill forms
        if not outreach_state["stop_requested"]:
            stats = await run_form_filling(leads, max_concurrent=3)
            outreach_state["total_processed"] = stats.get("total", 0)
            outreach_state["success"] = stats.get("success", 0)
            outreach_state["failed"] = stats.get("failed", 0)
            outreach_state["no_form"] = stats.get("no_form", 0)

        _finalize()
        logger.info(f"Start Batch complete: {outreach_state['success']} success, {outreach_state['failed']} failed")
        return {"total": outreach_state["total_processed"], "success": outreach_state["success"], "failed": outreach_state["failed"]}

    except Exception as e:
        logger.error(f"Start Batch error: {e}")
        outreach_state["error"] = str(e)[:300]
        _finalize()
        return {"error": str(e)}


def run_form_outreach(batch_size: int = 10) -> dict:
    return asyncio.run(run_form_outreach_async(batch_size))


# ═══════════════════════════════════════════════════════════════
# RESTART — Retry stuck "processing" forms (manual only)
# ═══════════════════════════════════════════════════════════════

async def restart_form_outreach_async() -> dict:
    """Retry all stuck processing forms. Stop flag checked inside run_form_filling."""
    outreach_state["running"] = True
    outreach_state["restarting"] = True
    _reset_state()

    try:
        if not db:
            _finalize()
            return {"error": "No database connection"}

        processing_leads = db.select("leads", filters={
            "form_submission_status": "eq.processing",
        }, order="form_last_attempted_at.asc", limit=500)

        if not processing_leads:
            _finalize()
            return {"total": 0, "message": "No stuck forms found"}

        logger.info(f"Restart: {len(processing_leads)} stuck forms")

        # Process all at once — run_form_filling checks stop_requested between batches
        stats = await run_form_filling(processing_leads, max_concurrent=3)

        outreach_state["total_processed"] = stats.get("total", 0)
        outreach_state["success"] = stats.get("success", 0)
        outreach_state["failed"] = stats.get("failed", 0)
        outreach_state["no_form"] = stats.get("no_form", 0)

        _finalize()
        logger.info(f"Restart done: {outreach_state['success']} success, {outreach_state['failed']} failed")
        return {"total": outreach_state["total_processed"], "success": outreach_state["success"], "failed": outreach_state["failed"]}

    except Exception as e:
        logger.error(f"Restart error: {e}")
        outreach_state["error"] = str(e)[:300]
        _finalize()
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
# BACKGROUND THREAD CONTROL
# ═══════════════════════════════════════════════════════════════

def start_form_outreach_background(batch_size: int = 10) -> dict:
    global _worker_thread
    if outreach_state["running"]:
        return {"status": "already_running", "message": "Already running"}

    def worker():
        try:
            run_form_outreach(batch_size)
        except Exception as e:
            logger.error(f"Background error: {e}")
            outreach_state["error"] = str(e)[:300]
            _finalize()

    _worker_thread = threading.Thread(target=worker, daemon=True, name="form_outreach_worker")
    _worker_thread.start()
    return {"status": "started", "batch_size": batch_size}


def restart_form_outreach_background() -> dict:
    global _worker_thread
    if outreach_state["running"]:
        return {"status": "already_running", "message": "Already running — stop first"}

    processing_count = 0
    if db:
        try:
            processing_count = db.count("leads", {"form_submission_status": "eq.processing"})
        except Exception:
            pass

    if processing_count == 0:
        return {"status": "nothing_to_restart", "processing_count": 0, "message": "No stuck forms"}

    def worker():
        try:
            asyncio.run(restart_form_outreach_async())
        except Exception as e:
            logger.error(f"Background restart error: {e}")
            outreach_state["error"] = str(e)[:300]
            _finalize()

    _worker_thread = threading.Thread(target=worker, daemon=True, name="form_restart_worker")
    _worker_thread.start()
    return {"status": "restarting", "processing_count": processing_count}


def stop_form_outreach() -> dict:
    if not outreach_state["running"]:
        return {"status": "not_running"}
    outreach_state["stop_requested"] = True
    return {"status": "stop_requested", "message": "Stopping after current form..."}


def get_dashboard_results(limit: int = 50) -> list[dict]:
    return get_form_outreach_results(limit=limit)
