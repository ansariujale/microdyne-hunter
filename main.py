#!/usr/bin/env python3
"""
FlowLockHunter v2 — Main Orchestrator
Runs the full daily pipeline: scrape → qualify → email → form fill → follow-up.
Also handles weekly intelligence reports.

Usage:
    python main.py                  # Run full daily pipeline
    python main.py --scrape         # Scrape only
    python main.py --email          # Email only (already-scraped leads)
    python main.py --forms          # Form fill only
    python main.py --followup       # Send follow-ups only
    python main.py --report         # Generate intelligence report
    python main.py --schedule       # Run on daily schedule (cron)
    python main.py --stats          # Show current stats
"""

import sys
import time
import logging
import argparse
from datetime import datetime, timezone

import schedule
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.logging import RichHandler

from config import DAILY_LEAD_TARGET, WEEKLY_REPORT_DAY
from modules.database import (
    bulk_insert_leads, get_leads_for_email, get_leads_for_form_fill,
    get_followup_due, get_hot_leads, get_today_stats, get_total_leads,
)
from modules.scraper import run_daily_scrape
from modules.qualifier import qualify_leads
from modules.emailer import send_initial_emails, send_followup_emails
from modules.form_filler import fill_forms_sync
from modules.intelligence import generate_weekly_report, format_report_text
from modules.notifier import send_daily_summary, send_weekly_report_email

# ═══════════════════════════════════════════════════════════════
# LOGGING SETUP
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)],
)
logger = logging.getLogger("flowlockhunter")
console = Console()


# ═══════════════════════════════════════════════════════════════
# PIPELINE STEPS
# ═══════════════════════════════════════════════════════════════

def step_scrape() -> list[dict]:
    """Step 1: Scrape new leads."""
    console.print(Panel("Step 1: SCRAPE — Finding new leads", style="bold green"))
    raw_leads = run_daily_scrape()
    console.print(f"  Found {len(raw_leads)} raw unique leads")
    return raw_leads


def step_qualify(leads: list[dict]) -> list[dict]:
    """Step 2: Score and qualify leads."""
    console.print(Panel("Step 2: QUALIFY — Scoring leads with AI", style="bold blue"))
    qualified = qualify_leads(leads, use_ai=True)
    console.print(f"  Qualified {len(qualified)} leads (from {len(leads)} raw)")
    return qualified


def step_store(leads: list[dict]) -> tuple[int, int]:
    """Step 3: Store qualified leads in Supabase (with dedup)."""
    console.print(Panel("Step 3: STORE — Leads already inserted during scrape", style="bold cyan"))
    total = get_total_leads()
    console.print(f"  DB has {total} total leads (leads are inserted per-country during scrape)")
    return total, 0


def step_email(campaign_id: str = None) -> int:
    """Step 4: Send cold emails to new leads."""
    console.print(Panel("Step 4: EMAIL — Sending personalized cold emails", style="bold magenta"))
    leads = get_leads_for_email(limit=1000)
    if not leads:
        console.print("  No leads pending email — skipping")
        return 0
    sent = send_initial_emails(leads, campaign_id=campaign_id)
    console.print(f"  Sent {sent} initial emails")
    return sent


def step_forms() -> dict:
    """Step 5: Fill contact forms on lead websites."""
    console.print(Panel("Step 5: FORM FILL — Submitting website contact forms", style="bold yellow"))
    leads = get_leads_for_form_fill(limit=1000)
    if not leads:
        console.print("  No leads pending form fill — skipping")
        return {"success": 0, "no_form": 0, "failed": 0}
    stats = fill_forms_sync(leads, max_concurrent=3)
    console.print(f"  Forms: {stats['success']} success, {stats['no_form']} no form, {stats['failed']} failed")
    return stats


def step_followup(campaign_id: str = None) -> int:
    """Step 6: Send follow-up emails."""
    console.print(Panel("Step 6: FOLLOW UP — Sending sequence emails", style="bold red"))
    leads = get_followup_due()
    if not leads:
        console.print("  No follow-ups due today — skipping")
        return 0
    sent = send_followup_emails(leads, campaign_id=campaign_id)
    console.print(f"  Sent {sent} follow-up emails")
    return sent


def step_report():
    """Generate and send weekly intelligence report."""
    console.print(Panel("INTELLIGENCE REPORT — Weekly analysis", style="bold white"))
    report = generate_weekly_report()
    text = format_report_text(report)
    console.print(text)
    send_weekly_report_email(text)
    console.print("  Report saved and emailed")
    return report


# ═══════════════════════════════════════════════════════════════
# FULL DAILY PIPELINE
# ═══════════════════════════════════════════════════════════════

def run_daily_pipeline():
    """Run the complete daily FlowLockHunter pipeline."""
    start = time.time()
    console.print(Panel(
        "[bold]FlowLockHunter v2 — Daily Pipeline[/bold]\n"
        f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Target: {DAILY_LEAD_TARGET} leads/day",
        style="bold green",
    ))

    # Step 1: Scrape
    raw_leads = step_scrape()

    # Step 2: Qualify
    qualified = step_qualify(raw_leads)

    # Step 3: Store
    inserted, skipped = step_store(qualified)

    # Step 4: Send emails
    emails_sent = step_email()

    # Step 5: Fill forms
    form_stats = step_forms()

    # Step 6: Follow-ups
    followups_sent = step_followup()

    # Check if it's report day
    today = datetime.now(timezone.utc).strftime("%A").lower()
    if today == WEEKLY_REPORT_DAY:
        step_report()

    # Daily summary
    elapsed = time.time() - start
    stats = {
        "leads_added": inserted,
        "emails_sent": emails_sent,
        "forms_filled": form_stats.get("success", 0),
        "followups_sent": followups_sent,
    }

    console.print(Panel(
        f"[bold green]Pipeline Complete[/bold green]\n\n"
        f"Leads scraped:    {len(raw_leads)}\n"
        f"Qualified:        {len(qualified)}\n"
        f"New in database:  {inserted}\n"
        f"Emails sent:      {emails_sent}\n"
        f"Forms filled:     {form_stats.get('success', 0)}\n"
        f"Follow-ups:       {followups_sent}\n"
        f"Time:             {elapsed:.1f}s",
        title="Daily Summary",
        style="green",
    ))

    # Send daily summary notification
    send_daily_summary(stats)

    return stats


# ═══════════════════════════════════════════════════════════════
# STATS DISPLAY
# ═══════════════════════════════════════════════════════════════

def show_stats():
    """Display current database stats."""
    total = get_total_leads()
    today = get_today_stats()
    hot = get_hot_leads()

    table = Table(title="FlowLockHunter v2 — Current Stats")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Total Leads", f"{total:,}")
    table.add_row("Today: Leads Added", f"{today['leads_added']:,}")
    table.add_row("Today: Emails Sent", f"{today['emails_sent']:,}")
    table.add_row("Today: Forms Filled", f"{today['forms_filled']:,}")
    table.add_row("Hot Leads (pending)", f"{len(hot)}")

    console.print(table)

    if hot:
        console.print("\n[bold yellow]Hot Leads (replied + interested):[/bold yellow]")
        for lead in hot[:10]:
            console.print(
                f"  • {lead['company_name']} ({lead['country']}) — "
                f"{lead['contact_name']} <{lead['contact_email']}> — "
                f"Score: {lead['score']}"
            )


# ═══════════════════════════════════════════════════════════════
# SCHEDULER
# ═══════════════════════════════════════════════════════════════

def run_scheduled():
    """Run on a daily schedule."""
    console.print("[bold]FlowLockHunter v2 — Scheduled Mode[/bold]")
    console.print("Pipeline will run daily at 06:00 UTC\n")

    schedule.every().day.at("06:00").do(run_daily_pipeline)

    # Also schedule weekly report on Sundays
    schedule.every().sunday.at("20:00").do(step_report)

    while True:
        schedule.run_pending()
        time.sleep(60)


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="FlowLockHunter v2 — AI Sales Agent for FlowLock Overseas")
    parser.add_argument("--scrape", action="store_true", help="Run scraping only")
    parser.add_argument("--email", action="store_true", help="Send emails only")
    parser.add_argument("--forms", action="store_true", help="Fill forms only")
    parser.add_argument("--followup", action="store_true", help="Send follow-ups only")
    parser.add_argument("--report", action="store_true", help="Generate intelligence report")
    parser.add_argument("--schedule", action="store_true", help="Run on daily schedule")
    parser.add_argument("--stats", action="store_true", help="Show current stats")
    parser.add_argument("--campaign-id", type=str, help="Instantly campaign ID")

    args = parser.parse_args()

    if args.stats:
        show_stats()
    elif args.scrape:
        raw = step_scrape()
        qualified = step_qualify(raw)
        step_store(qualified)
    elif args.email:
        step_email(campaign_id=args.campaign_id)
    elif args.forms:
        step_forms()
    elif args.followup:
        step_followup(campaign_id=args.campaign_id)
    elif args.report:
        step_report()
    elif args.schedule:
        run_scheduled()
    else:
        # Full pipeline
        run_daily_pipeline()


if __name__ == "__main__":
    main()
