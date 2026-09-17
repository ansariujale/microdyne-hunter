"""
MicrodyneHunter v2 — Intelligence & Auto-Optimization Module
Generates weekly reports, tracks segment performance, auto-excludes dead segments.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from config import AUTO_EXCLUSION, NOTIFICATION_EMAIL
from modules.database import (
    db, get_segment_performance, upsert_segment_performance,
    save_report,
)

logger = logging.getLogger("microdynehunter.intelligence")


# ═══════════════════════════════════════════════════════════════
# SEGMENT PERFORMANCE CALCULATION
# ═══════════════════════════════════════════════════════════════

def calculate_segment_metrics() -> dict:
    """
    Calculate performance metrics for all segments.
    Returns a dict with metrics by country, lead_type, source, and channel.
    """
    if not db:
        return {"country": [], "lead_type": [], "source": [], "channel": []}

    report = {}

    # Build metrics per segment type
    for segment_type in ["country", "lead_type", "source"]:
        report[segment_type] = _calculate_segment(segment_type)

    # Channel performance (email vs form)
    report["channel"] = _calculate_channel_performance()

    return report


def _calculate_segment(segment_type: str) -> list[dict]:
    """Calculate metrics for a single segment dimension."""
    if not db:
        return []

    col = segment_type
    result = db.select("leads", columns=col, filters={"excluded": "eq.false"})
    unique_values = list(set(r[col] for r in result if r.get(col)))

    segments = []
    for value in unique_values:
        total = db.count("leads", {col: f"eq.{value}", "excluded": "eq.false"})
        emailed = db.count("leads", {col: f"eq.{value}", "email_sent": "eq.true"})
        forms = db.count("leads", {col: f"eq.{value}", "form_filled": "eq.true"})
        opens = db.count("leads", {col: f"eq.{value}", "email_opened": "eq.true"})
        replies = db.count("leads", {col: f"eq.{value}", "replied": "eq.true"})
        interested_ct = db.count("leads", {col: f"eq.{value}", "interested": "eq.true"})
        closed = db.count("leads", {col: f"eq.{value}", "closed": "eq.true"})

        # Revenue
        rev_result = db.select("leads", columns="revenue_monthly",
                               filters={col: f"eq.{value}", "closed": "eq.true"})
        revenue = sum(r.get("revenue_monthly", 0) or 0 for r in rev_result)

        open_rate = opens / emailed if emailed > 0 else 0
        reply_rate = replies / emailed if emailed > 0 else 0
        close_rate = closed / total if total > 0 else 0

        segment = {
            "segment_value": value,
            "total_leads": total,
            "emails_sent": emailed,
            "forms_filled": forms,
            "opens": opens,
            "replies": replies,
            "interested": interested_ct,
            "closed": closed,
            "revenue": float(revenue),
            "open_rate": round(open_rate, 4),
            "reply_rate": round(reply_rate, 4),
            "close_rate": round(close_rate, 4),
        }
        segments.append(segment)
        upsert_segment_performance(segment_type, value, segment)

    return sorted(segments, key=lambda x: x["close_rate"], reverse=True)


def _calculate_channel_performance() -> list[dict]:
    """Calculate email vs form fill performance."""
    if not db:
        return []
    channels = []

    for channel, filter_col in [("email", "email_sent"), ("form", "form_filled")]:
        total = db.count("leads", {filter_col: "eq.true"})
        replies = db.count("leads", {filter_col: "eq.true", "replied": "eq.true"})
        closed = db.count("leads", {filter_col: "eq.true", "closed": "eq.true"})

        reply_rate = replies / total if total > 0 else 0
        close_rate = closed / total if total > 0 else 0

        channels.append({
            "segment_value": channel,
            "total_leads": total,
            "replies": replies,
            "closed": closed,
            "reply_rate": round(reply_rate, 4),
            "close_rate": round(close_rate, 4),
        })

    # Both channels
    both = db.count("leads", {"email_sent": "eq.true", "form_filled": "eq.true"})
    both_replies = db.count("leads", {"email_sent": "eq.true", "form_filled": "eq.true", "replied": "eq.true"})

    channels.append({
        "segment_value": "both",
        "total_leads": both,
        "replies": both_replies,
        "reply_rate": round(both_replies / both, 4) if both > 0 else 0,
    })

    return channels


# ═══════════════════════════════════════════════════════════════
# AUTO-EXCLUSION ENGINE
# ═══════════════════════════════════════════════════════════════

def run_auto_exclusion() -> list[dict]:
    """
    Apply auto-exclusion rules based on segment performance.
    Returns list of actions taken.
    """
    actions = []
    rules = AUTO_EXCLUSION

    # Rule 1: Pause countries with 0 closes after N leads
    country_segments = get_segment_performance("country")
    for seg in country_segments:
        if (seg["total_leads"] >= rules["country_pause_after_leads"]
                and seg["closed"] == 0
                and not seg.get("is_paused")):
            _pause_segment("country", seg["segment_value"],
                           f"0 closes after {seg['total_leads']} leads contacted")
            actions.append({
                "action": "PAUSE_COUNTRY",
                "segment": seg["segment_value"],
                "reason": f"0 closes after {seg['total_leads']} leads",
            })
            logger.warning(f"AUTO-PAUSE: Country {seg['segment_value']} — 0 closes after {seg['total_leads']} leads")

    # Rule 2: Deprioritize lead types with low close rate
    type_segments = get_segment_performance("lead_type")
    for seg in type_segments:
        if (seg["total_leads"] >= rules["lead_type_min_sample"]
                and seg["close_rate"] < rules["lead_type_min_close_rate"]
                and not seg.get("is_paused")):
            _deprioritize_segment("lead_type", seg["segment_value"],
                                  f"Close rate {seg['close_rate']:.2%} below {rules['lead_type_min_close_rate']:.1%}")
            actions.append({
                "action": "DEPRIORITIZE_TYPE",
                "segment": seg["segment_value"],
                "close_rate": seg["close_rate"],
            })
            logger.warning(f"AUTO-DEPRIORITIZE: Lead type {seg['segment_value']} — {seg['close_rate']:.2%} close rate")

    # Rule 3: Reduce volume from low-reply sources
    source_segments = get_segment_performance("source")
    for seg in source_segments:
        if (seg.get("emails_sent", 0) >= rules["source_min_sample"]
                and seg["reply_rate"] < rules["source_min_reply_rate"]
                and not seg.get("is_paused")):
            _deprioritize_segment("source", seg["segment_value"],
                                  f"Reply rate {seg['reply_rate']:.2%} below {rules['source_min_reply_rate']:.1%}")
            actions.append({
                "action": "REDUCE_SOURCE",
                "segment": seg["segment_value"],
                "reply_rate": seg["reply_rate"],
            })

    logger.info(f"Auto-exclusion complete: {len(actions)} actions taken")
    return actions


def _pause_segment(segment_type: str, segment_value: str, reason: str):
    """Pause a segment (stop targeting it entirely)."""
    upsert_segment_performance(segment_type, segment_value, {
        "is_paused": True,
        "pause_reason": reason,
        "priority_score": 0,
    })


def _deprioritize_segment(segment_type: str, segment_value: str, reason: str):
    """Reduce priority of a segment (lower volume, not fully paused)."""
    upsert_segment_performance(segment_type, segment_value, {
        "priority_score": 10,
        "pause_reason": reason,
    })


# ═══════════════════════════════════════════════════════════════
# WEEKLY INTELLIGENCE REPORT
# ═══════════════════════════════════════════════════════════════

def generate_weekly_report() -> dict:
    """
    Generate the full weekly intelligence report.
    Called every Sunday by the orchestrator.
    """
    logger.info("Generating weekly intelligence report...")

    # Calculate all metrics
    metrics = calculate_segment_metrics()

    # Run auto-exclusion
    actions = run_auto_exclusion()

    # Get overall stats
    if db:
        total_leads = db.count("leads")
        total_emailed = db.count("leads", {"email_sent": "eq.true"})
        total_forms = db.count("leads", {"form_filled": "eq.true"})
        total_replies = db.count("leads", {"replied": "eq.true"})
        total_interested = db.count("leads", {"interested": "eq.true"})
        total_closed = db.count("leads", {"closed": "eq.true"})

        rev_result = db.select("leads", columns="revenue_monthly",
                               filters={"closed": "eq.true"})
        total_revenue = sum(r.get("revenue_monthly", 0) or 0 for r in rev_result)
    else:
        total_leads = total_emailed = total_forms = total_replies = 0
        total_interested = total_closed = 0
        total_revenue = 0

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_leads": total_leads,
            "total_emailed": total_emailed,
            "total_forms_filled": total_forms,
            "total_replies": total_replies,
            "total_interested": total_interested,
            "total_closed": total_closed,
            "total_mrr": float(total_revenue),
            "overall_reply_rate": round(total_replies / total_emailed, 4) if total_emailed > 0 else 0,
            "overall_close_rate": round(total_closed / total_leads, 4) if total_leads > 0 else 0,
        },
        "by_country": metrics.get("country", []),
        "by_lead_type": metrics.get("lead_type", []),
        "by_source": metrics.get("source", []),
        "by_channel": metrics.get("channel", []),
        "auto_actions": actions,
        "top_performing": {
            "best_country": metrics["country"][0]["segment_value"] if metrics.get("country") else "N/A",
            "best_lead_type": metrics["lead_type"][0]["segment_value"] if metrics.get("lead_type") else "N/A",
            "best_source": metrics["source"][0]["segment_value"] if metrics.get("source") else "N/A",
        },
    }

    # Save to database
    save_report("weekly", report, {"actions": actions})

    logger.info(f"Weekly report generated: {total_leads} leads, {total_closed} closed, ${total_revenue:.0f} MRR")
    return report


def format_report_text(report: dict) -> str:
    """Format the report as readable text (for email notification)."""
    s = report["summary"]
    lines = [
        "=" * 60,
        "MICRODYNEHUNTER v2 — WEEKLY INTELLIGENCE REPORT",
        f"Generated: {report['generated_at'][:10]}",
        "=" * 60,
        "",
        "OVERALL SUMMARY",
        f"  Total Leads:     {s['total_leads']:,}",
        f"  Emails Sent:     {s['total_emailed']:,}",
        f"  Forms Filled:    {s['total_forms_filled']:,}",
        f"  Replies:         {s['total_replies']:,} ({s['overall_reply_rate']:.1%})",
        f"  Interested:      {s['total_interested']:,}",
        f"  Closed Deals:    {s['total_closed']:,} ({s['overall_close_rate']:.1%})",
        f"  Monthly Revenue: ${s['total_mrr']:,.0f}",
        "",
        "TOP PERFORMERS",
        f"  Best Country:    {report['top_performing']['best_country']}",
        f"  Best Lead Type:  {report['top_performing']['best_lead_type']}",
        f"  Best Source:     {report['top_performing']['best_source']}",
        "",
    ]

    lines.append("BY COUNTRY (top 10)")
    lines.append("-" * 50)
    for seg in report.get("by_country", [])[:10]:
        lines.append(
            f"  {seg['segment_value']:15s} | "
            f"{seg['total_leads']:5d} leads | "
            f"{seg['replies']:3d} replies ({seg['reply_rate']:.1%}) | "
            f"{seg['closed']:2d} closed ({seg['close_rate']:.1%}) | "
            f"${seg['revenue']:,.0f}"
        )

    lines.append("")

    if report.get("auto_actions"):
        lines.append("AUTO-OPTIMIZATION ACTIONS")
        lines.append("-" * 50)
        for action in report["auto_actions"]:
            lines.append(f"  {action['action']}: {action['segment']} — {action.get('reason', '')}")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)
