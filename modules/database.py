"""
FlowLockHunter v2 — Database Module (Lightweight)
Uses httpx to call Supabase REST API directly — no heavy SDK needed.
"""

import json
import logging
import uuid
from typing import Optional
from datetime import datetime, timedelta, timezone

import httpx
from config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger("flowlockhunter.db")

# ═══════════════════════════════════════════════════════════════
# SUPABASE REST CLIENT (lightweight, no SDK)
# ═══════════════════════════════════════════════════════════════

class SupabaseREST:
    """Lightweight Supabase REST API client using httpx."""

    def __init__(self, url: str, key: str):
        self.base_url = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        self.client = httpx.Client(headers=self.headers, timeout=30)

    def select(self, table: str, columns: str = "*", filters: dict = None,
               order: str = None, limit: int = None) -> list[dict]:
        """SELECT from a table with optional filters."""
        url = f"{self.base_url}/{table}?select={columns}"
        if filters:
            for key, val in filters.items():
                url += f"&{key}={val}"
        if order:
            url += f"&order={order}"
        if limit:
            url += f"&limit={limit}"
        try:
            resp = self.client.get(url)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"SELECT {table} error: {e}")
            return []

    def insert(self, table: str, data: dict) -> Optional[dict]:
        """INSERT a row into a table."""
        url = f"{self.base_url}/{table}"
        try:
            resp = self.client.post(url, json=data)
            resp.raise_for_status()
            result = resp.json()
            inserted = result[0] if isinstance(result, list) and result else result
            if table == "leads":
                logger.info(f"[DB] Inserted lead: {data.get('company_domain', '?')} ({data.get('country', '?')})")
            return inserted
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:  # conflict = duplicate
                if table == "leads":
                    logger.debug(f"[DB] Duplicate lead (409): {data.get('company_domain', '?')}")
                return None
            logger.error(f"INSERT {table} error: {e.response.status_code} {e.response.text[:300]}")
            return None
        except Exception as e:
            logger.error(f"INSERT {table} error: {e}")
            return None

    def update(self, table: str, data: dict, filters: dict) -> Optional[dict]:
        """UPDATE rows matching filters."""
        url = f"{self.base_url}/{table}"
        for key, val in filters.items():
            url += f"{'?' if '?' not in url else '&'}{key}={val}"
        try:
            resp = self.client.patch(url, json=data)
            resp.raise_for_status()
            result = resp.json()
            return result[0] if isinstance(result, list) and result else result
        except Exception as e:
            logger.error(f"UPDATE {table} error: {e}")
            return None

    def delete(self, table: str, filters: dict) -> bool:
        """DELETE rows matching filters."""
        url = f"{self.base_url}/{table}"
        for key, val in filters.items():
            url += f"{'?' if '?' not in url else '&'}{key}={val}"
        try:
            resp = self.client.delete(url)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"DELETE {table} error: {e}")
            return False

    def count(self, table: str, filters: dict = None) -> int:
        """COUNT rows in a table."""
        url = f"{self.base_url}/{table}?select=id"
        headers = {**self.headers, "Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"}
        if filters:
            for key, val in filters.items():
                url += f"&{key}={val}"
        try:
            resp = self.client.get(url, headers=headers)
            content_range = resp.headers.get("content-range", "")
            # Format: "0-0/123" or "*/0"
            if "/" in content_range:
                total = content_range.split("/")[-1]
                return int(total) if total != "*" else 0
            return 0
        except Exception as e:
            logger.error(f"COUNT {table} error: {e}")
            return 0

    def rpc(self, function_name: str, params: dict = None) -> list[dict]:
        """Call a Supabase RPC function."""
        url = f"{self.base_url.replace('/rest/v1', '/rest/v1/rpc')}/{function_name}"
        try:
            resp = self.client.post(url, json=params or {})
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"RPC {function_name} error: {e}")
            return []


# ═══════════════════════════════════════════════════════════════
# IN-MEMORY DATABASE (Fallback when Supabase unavailable)
# ═══════════════════════════════════════════════════════════════

class InMemoryDB:
    """In-memory database with PostgREST-compatible filter syntax."""

    def __init__(self):
        self.tables = {
            "leads": [],
            "source_tracker": [],
            "segment_performance": [],
            "outreach_log": [],
            "intelligence_reports": [],
            "email_variants": [],
            "email_warmup": [],
        }

    def _apply_filters(self, rows: list[dict], filters: dict) -> list[dict]:
        """Apply PostgREST-style filters to rows."""
        if not filters:
            return rows

        result = []
        for row in rows:
            match = True
            for key, condition in filters.items():
                if not isinstance(condition, str):
                    match = False
                    break

                # Parse PostgREST operators: eq., neq., gte., lte., lt., in.()
                if condition.startswith("eq."):
                    val = condition[3:]
                    if val == "" and row.get(key) != "":
                        match = False
                    elif val != "" and str(row.get(key)) != val:
                        match = False
                elif condition.startswith("neq."):
                    val = condition[4:]
                    if str(row.get(key)) == val:
                        match = False
                elif condition.startswith("gte."):
                    val = condition[4:]
                    try:
                        if float(row.get(key, 0)) < float(val):
                            match = False
                    except (ValueError, TypeError):
                        if str(row.get(key, "")) < val:
                            match = False
                elif condition.startswith("lte."):
                    val = condition[4:]
                    try:
                        if float(row.get(key, 0)) > float(val):
                            match = False
                    except (ValueError, TypeError):
                        if str(row.get(key, "")) > val:
                            match = False
                elif condition.startswith("lt."):
                    val = condition[3:]
                    try:
                        if float(row.get(key, 0)) >= float(val):
                            match = False
                    except (ValueError, TypeError):
                        if str(row.get(key, "")) >= val:
                            match = False
                elif condition.startswith("in.(") and condition.endswith(")"):
                    val_str = condition[4:-1]
                    vals = [v.strip().strip('"') for v in val_str.split(",")]
                    if str(row.get(key)) not in vals:
                        match = False
                else:
                    # Assume direct equality if no operator
                    if str(row.get(key)) != condition:
                        match = False

                if not match:
                    break

            if match:
                result.append(row)

        return result

    def _apply_order(self, rows: list[dict], order: str) -> list[dict]:
        """Apply PostgREST-style ordering: 'column.asc' or 'column.desc'."""
        if not order:
            return rows

        parts = order.split(".")
        if len(parts) != 2:
            return rows

        column, direction = parts
        reverse = direction.lower() == "desc"

        try:
            return sorted(rows, key=lambda r: r.get(column, ""), reverse=reverse)
        except Exception:
            return rows

    def select(self, table: str, columns: str = "*", filters: dict = None,
               order: str = None, limit: int = None) -> list[dict]:
        """SELECT from a table with optional filters."""
        if table not in self.tables:
            logger.warning(f"Table {table} does not exist in in-memory DB")
            return []

        rows = self.tables[table][:]
        rows = self._apply_filters(rows, filters)
        rows = self._apply_order(rows, order)

        if limit:
            rows = rows[:limit]

        # Filter columns if not "*"
        if columns != "*":
            cols = [c.strip() for c in columns.split(",")]
            rows = [{k: v for k, v in row.items() if k in cols} for row in rows]

        return rows

    def insert(self, table: str, data: dict) -> Optional[dict]:
        """INSERT a row into a table. Auto-generates UUID id."""
        if table not in self.tables:
            logger.warning(f"Table {table} does not exist in in-memory DB")
            return None

        row = data.copy()
        if "id" not in row:
            row["id"] = str(uuid.uuid4())

        self.tables[table].append(row)
        logger.debug(f"Inserted into {table}: {row['id']}")
        return row

    def update(self, table: str, data: dict, filters: dict) -> Optional[dict]:
        """UPDATE rows matching filters."""
        if table not in self.tables:
            logger.warning(f"Table {table} does not exist in in-memory DB")
            return None

        rows = self._apply_filters(self.tables[table], filters)
        if not rows:
            return None

        # Update the first matching row (matches Supabase behavior)
        row_to_update = rows[0]
        row_to_update.update(data)
        logger.debug(f"Updated {table}: {row_to_update.get('id')}")
        return row_to_update

    def count(self, table: str, filters: dict = None) -> int:
        """COUNT rows in a table."""
        if table not in self.tables:
            logger.warning(f"Table {table} does not exist in in-memory DB")
            return 0

        rows = self._apply_filters(self.tables[table], filters)
        return len(rows)

    def rpc(self, function_name: str, params: dict = None) -> list[dict]:
        """RPC calls are not supported in in-memory DB."""
        logger.warning(f"RPC {function_name} not supported in in-memory DB")
        return []


# Initialize client — only connect if real credentials are provided
def _is_valid_supabase_url(url: str) -> bool:
    """Check if this looks like a real Supabase URL (not a placeholder)."""
    if not url:
        return False
    url = url.strip().lower()
    placeholders = ["your-project", "your_project", "xxx", "placeholder", "example"]
    if any(p in url for p in placeholders):
        return False
    if not url.startswith("https://"):
        return False
    return True

if _is_valid_supabase_url(SUPABASE_URL) and SUPABASE_KEY and "your" not in SUPABASE_KEY.lower():
    try:
        db = SupabaseREST(SUPABASE_URL, SUPABASE_KEY)
        logger.info(f"Supabase connected: {SUPABASE_URL}")
    except Exception as e:
        logger.warning(f"Supabase init failed: {e} — falling back to in-memory database")
        db = InMemoryDB()
else:
    db = InMemoryDB()
    if SUPABASE_URL:
        logger.warning("Supabase URL looks like a placeholder — using in-memory database. Edit .env with real credentials.")
    else:
        logger.info("No Supabase URL configured — using in-memory database (leads stored in memory only)")


# ═══════════════════════════════════════════════════════════════
# DEDUPLICATION
# ═══════════════════════════════════════════════════════════════

def domain_exists(company_domain: str) -> bool:
    """Check if a company domain already exists in the database."""
    if not db:
        return False
    result = db.select("leads", columns="id",
                       filters={"company_domain": f"eq.{company_domain.lower().strip()}"},
                       limit=1)
    return len(result) > 0


def bulk_check_domains(domains: list[str]) -> set[str]:
    """Check which domains already exist. Returns set of existing domains."""
    if not db or not domains:
        return set()
    clean = [d.lower().strip() for d in domains]
    # Supabase uses `in` operator with parentheses
    in_list = ",".join(f'"{d}"' for d in clean)
    result = db.select("leads", columns="company_domain",
                       filters={"company_domain": f"in.({in_list})"})
    return {r["company_domain"] for r in result}


# ═══════════════════════════════════════════════════════════════
# LEAD CRUD
# ═══════════════════════════════════════════════════════════════

def insert_lead(lead: dict) -> Optional[dict]:
    """Insert a new lead. Returns the inserted row or None if duplicate."""
    if not db:
        return None
    lead["company_domain"] = lead["company_domain"].lower().strip()

    if domain_exists(lead["company_domain"]):
        logger.debug(f"Duplicate skipped: {lead['company_domain']}")
        return None

    return db.insert("leads", lead)


def bulk_insert_leads(leads: list[dict]) -> tuple[int, int]:
    """Insert multiple leads, skipping duplicates. Returns (inserted, skipped)."""
    if not leads:
        logger.warning("[DB] bulk_insert_leads called with 0 leads")
        return 0, 0

    logger.info(f"[DB] bulk_insert_leads: {len(leads)} leads to process")
    logger.info(f"[DB] Using database: {type(db).__name__}")

    domains = [l["company_domain"].lower().strip() for l in leads]
    existing = bulk_check_domains(domains)
    logger.info(f"[DB] Found {len(existing)} existing domains in DB")

    new_leads = []
    skipped = 0
    for lead in leads:
        domain = lead["company_domain"].lower().strip()
        lead["company_domain"] = domain
        if domain in existing:
            skipped += 1
        else:
            new_leads.append(lead)
            existing.add(domain)

    logger.info(f"[DB] {len(new_leads)} new leads to insert, {skipped} pre-existing duplicates")

    inserted = 0
    errors = 0
    for lead in new_leads:
        result = insert_lead(lead)
        if result:
            inserted += 1
        else:
            errors += 1

    logger.info(f"[DB] Bulk insert complete: {inserted} inserted, {skipped} skipped, {errors} errors")
    return inserted, skipped + errors


def update_lead(lead_id: str, updates: dict) -> Optional[dict]:
    """Update a lead by ID."""
    if not db:
        return None
    return db.update("leads", updates, {"id": f"eq.{lead_id}"})


def get_leads_for_email(limit: int = 1000) -> list[dict]:
    """Get qualified leads that haven't been emailed yet."""
    if not db:
        return []
    return db.select("leads", filters={
        "email_sent": "eq.false",
        "excluded": "eq.false",
        "score": "gte.40",
        "contact_email": "neq.",
    }, order="score.desc", limit=limit)


def get_leads_for_form_fill(limit: int = 1000) -> list[dict]:
    """Get leads that haven't had their contact form filled."""
    if not db:
        return []
    return db.select("leads", filters={
        "form_filled": "eq.false",
        "excluded": "eq.false",
        "has_contact_form": "eq.true",
        "score": "gte.40",
    }, order="score.desc", limit=limit)


# ═══════════════════════════════════════════════════════════════
# FORM OUTREACH QUERIES
# ═══════════════════════════════════════════════════════════════

def get_leads_for_form_outreach(limit: int = 10) -> list[dict]:
    """Get leads pending form outreach — never attempted before.
    Skips any lead whose website_url was already used in a previous attempt."""
    if not db:
        return []

    # Step 1: Get candidate leads (pending + never filled + score >= 40)
    from config import SCORE_THRESHOLDS
    min_score = SCORE_THRESHOLDS.get("min_qualify", 40)
    candidates = db.select("leads", filters={
        "form_submission_status": "eq.pending",
        "form_filled": "eq.false",
        "excluded": "eq.false",
        "score": f"gte.{min_score}",
    }, order="created_at.asc", limit=limit * 3)  # fetch extra to allow filtering

    if not candidates:
        return []

    # Step 2: Get all website_urls that have already been attempted
    attempted = db.select("leads",
        columns="website_url",
        filters={"form_submission_status": "neq.pending"},
        limit=10000
    )
    attempted_urls = set()
    for row in (attempted or []):
        url = (row.get("website_url") or "").strip().lower().rstrip("/")
        if url:
            attempted_urls.add(url)

    # Step 3: Filter out candidates whose website was already attempted
    fresh_leads = []
    seen_urls = set()
    for lead in candidates:
        url = (lead.get("website_url") or "").strip().lower().rstrip("/")
        if not url:
            continue
        if url in attempted_urls:
            # Mark this lead as already done so it's not picked again
            update_form_status(lead["id"], "failed", error_msg="Website already submitted previously")
            continue
        if url in seen_urls:
            # Duplicate within this batch
            update_form_status(lead["id"], "failed", error_msg="Duplicate website in batch")
            continue
        seen_urls.add(url)
        fresh_leads.append(lead)
        if len(fresh_leads) >= limit:
            break

    return fresh_leads


def update_form_status(lead_id: str, status: str, error_msg: str = None,
                       form_url: str = None) -> None:
    """Update form outreach status for a lead."""
    if not db:
        return
    updates = {
        "form_submission_status": status,
        "form_last_attempted_at": datetime.now(timezone.utc).isoformat(),
    }
    if error_msg is not None:
        updates["form_error_message"] = error_msg
    if form_url is not None:
        updates["contact_page_url"] = form_url
    db.update("leads", updates, {"id": f"eq.{lead_id}"})


def get_funnel_counts() -> dict:
    """Get real funnel counts from database for the sales funnel page."""
    if not db:
        return {}
    try:
        total = db.count("leads")
        qualified = db.count("leads", {"score": "gte.40"})
        emailed = db.count("leads", {"email_sent": "eq.true"})
        form_filled = db.count("leads", {"form_filled": "eq.true"})
        opened = db.count("leads", {"email_opened": "eq.true"})
        replied = db.count("leads", {"replied": "eq.true"})
        interested = db.count("leads", {"interested": "eq.true"})
        closed = db.count("leads", {"closed": "eq.true"})

        # Revenue from closed deals
        closed_leads = db.select("leads", columns="revenue_monthly",
                                 filters={"closed": "eq.true"})
        mrr = sum(r.get("revenue_monthly", 0) or 0 for r in (closed_leads or []))

        return {
            "total": total,
            "qualified": qualified,
            "emailed": emailed,
            "form_filled": form_filled,
            "opened": opened,
            "replied": replied,
            "interested": interested,
            "closed": closed,
            "mrr": mrr,
        }
    except Exception as e:
        logger.error(f"Error getting funnel counts: {e}")
        return {}


def get_form_outreach_counts() -> dict:
    """Get total form outreach counts from database for dashboard stats."""
    if not db:
        return {"success": 0, "failed": 0, "no_form": 0, "processing": 0, "pending": 0, "total": 0}

    try:
        success = db.count("leads", {"form_submission_status": "eq.success"})
        failed = db.count("leads", {"form_submission_status": "eq.failed"})
        processing = db.count("leads", {"form_submission_status": "eq.processing"})
        pending = db.count("leads", {"form_submission_status": "eq.pending"})

        # No form = failed with specific error message
        no_form_leads = db.select("leads",
            columns="id",
            filters={
                "form_submission_status": "eq.failed",
                "form_error_message": "eq.No contact form found",
            }, limit=10000)
        no_form = len(no_form_leads) if no_form_leads else 0

        return {
            "success": success,
            "failed": failed - no_form,  # failed excluding no_form
            "no_form": no_form,
            "processing": processing,
            "pending": pending,
            "total": success + failed,  # total = success + all failed (including no_form)
        }
    except Exception as e:
        logger.error(f"Error getting form outreach counts: {e}")
        return {"success": 0, "failed": 0, "no_form": 0, "processing": 0, "pending": 0, "total": 0}


def get_form_outreach_results(limit: int = 50, status: str = None,
                              date_from: str = None, date_to: str = None,
                              search: str = None) -> list[dict]:
    """Get form outreach results for dashboard with optional filters.

    Args:
        limit: max rows to return
        status: filter by form_submission_status (e.g. 'success', 'failed')
        date_from: ISO date string, only results on or after this date
        date_to: ISO date string, only results on or before this date
        search: text search against company_name or website_url
    """
    if not db:
        return []
    filters = {
        "form_submission_status": "neq.pending",
    }
    if status and status in ("success", "failed", "processing"):
        filters["form_submission_status"] = f"eq.{status}"
    if date_from:
        filters["form_last_attempted_at"] = f"gte.{date_from}"
    if date_to:
        # PostgREST uses separate keys for range — we need a unique key
        # Use lte. on the same column. Supabase REST supports multiple
        # filters on the same column via `and` or by passing both.
        # For simplicity we combine with date_from using `and` syntax.
        if date_from:
            # Both from and to — use 'and' filter syntax
            filters.pop("form_last_attempted_at", None)
            filters["and"] = f"(form_last_attempted_at.gte.{date_from},form_last_attempted_at.lte.{date_to}T23:59:59Z)"
        else:
            filters["form_last_attempted_at"] = f"lte.{date_to}T23:59:59Z"

    results = db.select("leads",
        columns="id,company_name,website_url,contact_page_url,form_submission_status,form_error_message,form_last_attempted_at,form_filled,replied,reply_source",
        filters=filters,
        order="form_last_attempted_at.desc",
        limit=limit
    )

    # Client-side text search (PostgREST ilike requires specific setup)
    if search and results:
        s = search.lower()
        results = [r for r in results
                   if s in (r.get("company_name") or "").lower()
                   or s in (r.get("website_url") or "").lower()]

    return results


def get_followup_due() -> list[dict]:
    """Get leads needing follow-up emails today."""
    if not db:
        return []
    now = datetime.now(timezone.utc).isoformat()
    return db.select("leads", filters={
        "replied": "eq.false",
        "excluded": "eq.false",
        "sequence_stage": "lt.4",
        "next_followup": f"lte.{now}",
    }, order="score.desc")


def get_hot_leads() -> list[dict]:
    """Get hot leads — replied, opened with high score, or interested. Sorted by priority."""
    if not db:
        return []
    hot = []
    # Priority 1: Replied leads (highest)
    replied = db.select("leads", filters={
        "replied": "eq.true",
        "closed": "eq.false",
    }, order="replied_at.desc", limit=20)
    hot.extend(replied)

    # Priority 2: Opened + high score (score >= 70)
    seen_ids = {l["id"] for l in hot}
    opened = db.select("leads", filters={
        "email_opened": "eq.true",
        "replied": "eq.false",
        "closed": "eq.false",
        "score": "gte.70",
    }, order="score.desc", limit=20)
    for l in opened:
        if l["id"] not in seen_ids:
            hot.append(l)
            seen_ids.add(l["id"])

    # Priority 3: Interested leads
    interested = db.select("leads", filters={
        "interested": "eq.true",
        "closed": "eq.false",
    }, order="score.desc", limit=10)
    for l in interested:
        if l["id"] not in seen_ids:
            hot.append(l)
            seen_ids.add(l["id"])

    return hot[:30]


# ═══════════════════════════════════════════════════════════════
# SOURCE EXHAUSTION TRACKER
# ═══════════════════════════════════════════════════════════════

def get_active_sources() -> list[dict]:
    """Get all active (non-exhausted) source/keyword combos."""
    if not db:
        return []
    return db.select("source_tracker", filters={"status": "eq.active"},
                     order="last_scraped.asc")


def update_source_tracker(source: str, keyword: str, country: str,
                          city: str = None, lead_type: str = None,
                          new_found: int = 0) -> None:
    """Update or create a source tracker entry."""
    if not db:
        return
    existing = db.select("source_tracker", filters={
        "source": f"eq.{source}",
        "keyword": f"eq.{keyword}",
        "country": f"eq.{country}",
    })

    now = datetime.now(timezone.utc).isoformat()

    if existing:
        entry = existing[0]
        total = entry["total_found"] + new_found
        status = "exhausted" if new_found == 0 else "active"
        db.update("source_tracker", {
            "total_found": total,
            "last_batch_new": new_found,
            "status": status,
            "last_scraped": now,
        }, {"id": f"eq.{entry['id']}"})
    else:
        data = {
            "source": source, "keyword": keyword, "country": country,
            "city": city or "", "total_found": new_found,
            "last_batch_new": new_found, "last_scraped": now,
            "status": "active" if new_found > 0 else "exhausted",
        }
        if lead_type:
            data["lead_type"] = lead_type
        db.insert("source_tracker", data)


# ═══════════════════════════════════════════════════════════════
# OUTREACH LOG
# ═══════════════════════════════════════════════════════════════

def log_outreach(lead_id: str, channel: str, sequence_stage: int = 1,
                 subject: str = None, sending_domain: str = None,
                 form_url: str = None, form_submitted: bool = False) -> None:
    """Log an outreach action (email or form fill)."""
    if not db:
        return
    db.insert("outreach_log", {
        "lead_id": lead_id, "channel": channel,
        "sequence_stage": sequence_stage, "subject": subject,
        "sending_domain": sending_domain, "form_url": form_url,
        "form_submitted": form_submitted,
    })


# ═══════════════════════════════════════════════════════════════
# SEGMENT PERFORMANCE
# ═══════════════════════════════════════════════════════════════

def get_segment_performance(segment_type: str = None) -> list[dict]:
    """Get segment performance data, optionally filtered by type."""
    if not db:
        return []
    filters = {}
    if segment_type:
        filters["segment_type"] = f"eq.{segment_type}"
    return db.select("segment_performance", filters=filters,
                     order="close_rate.desc")


def is_segment_paused(segment_type: str, segment_value: str) -> bool:
    """Check if a segment is paused by the auto-exclusion system."""
    if not db:
        return False
    result = db.select("segment_performance", columns="is_paused", filters={
        "segment_type": f"eq.{segment_type}",
        "segment_value": f"eq.{segment_value}",
    })
    if result:
        return result[0].get("is_paused", False)
    return False


def upsert_segment_performance(segment_type: str, segment_value: str,
                                metrics: dict) -> None:
    """Update or create segment performance record."""
    if not db:
        return
    existing = db.select("segment_performance", filters={
        "segment_type": f"eq.{segment_type}",
        "segment_value": f"eq.{segment_value}",
    })

    data = {
        "segment_type": segment_type,
        "segment_value": segment_value,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        **metrics,
    }

    if existing:
        db.update("segment_performance", data, {"id": f"eq.{existing[0]['id']}"})
    else:
        db.insert("segment_performance", data)


# ═══════════════════════════════════════════════════════════════
# INTELLIGENCE REPORTS
# ═══════════════════════════════════════════════════════════════

def save_report(report_type: str, report_data: dict, actions: dict = None) -> None:
    """Save an intelligence report."""
    if not db:
        return
    db.insert("intelligence_reports", {
        "report_type": report_type,
        "report_data": json.dumps(report_data),
        "actions_taken": json.dumps(actions) if actions else None,
    })


# ═══════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════

def get_total_leads() -> int:
    if not db:
        return 0
    return db.count("leads")


def get_today_stats() -> dict:
    """Get today's lead/outreach counts."""
    if not db:
        return {"leads_added": 0, "emails_sent": 0, "forms_filled": 0}
    now_local = datetime.now()
    today = now_local.date().isoformat()

    def _is_today(ts: str) -> bool:
        if not ts:
            return False
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
            return d == today
        except Exception:
            return False

    leads = db.select("leads", columns="created_at,email_sent,email_sent_at,form_filled,form_filled_at", limit=10000) or []
    leads_added = sum(1 for r in leads if _is_today(r.get("created_at", "")))
    emails_sent = sum(1 for r in leads if r.get("email_sent") and _is_today(r.get("email_sent_at", "")))
    forms_filled = sum(1 for r in leads if r.get("form_filled") and _is_today(r.get("form_filled_at", "")))

    return {
        "leads_added": leads_added,
        "emails_sent": emails_sent,
        "forms_filled": forms_filled,
    }
