"""
MicrodyneHunter v2 — Lead Scraping Module
Primary source: Apify Google Maps Scraper (compass/crawler-google-places)
Also supports: Apollo.io, DuckDuckGo/Bing fallback
"""

import re
import time
import logging
from typing import Optional
from urllib.parse import urlparse, quote_plus

import httpx
from bs4 import BeautifulSoup

from config import (
    APIFY_API_KEY, APOLLO_API_KEY, APOLLO_JOB_TITLES,
    SCRAPE_DELAY_SECONDS, REQUEST_TIMEOUT, MAX_RETRIES,
)
from modules.database import (
    bulk_check_domains, update_source_tracker, is_segment_paused,
)
from modules.events import emit_log, get_country_flag

logger = logging.getLogger("microdynehunter.scraper")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SKIP_DOMAINS = [
    "google.", "youtube.", "wikipedia.", "facebook.", "linkedin.",
    "twitter.", "reddit.", "yelp.", "bloomberg.", "crunchbase.",
    "amazon.", "instagram.", "tiktok.", "pinterest.", "quora.",
    "stackoverflow.", "github.", "medium.", "apple.", "microsoft.",
    "x.com", "bbb.org", "glassdoor.", "indeed.", "trustpilot.",
]


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def extract_domain(url: str) -> Optional[str]:
    if not url:
        return None
    try:
        if not url.startswith("http"):
            url = "https://" + url
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")
        return domain if domain else None
    except Exception:
        return None


def extract_state_from_address(address: str, country: str = "") -> str:
    """
    Extract state/province from a full address string.
    Google Maps addresses typically end with: ..., City, State ZIP, Country
    """
    if not address:
        return ""

    # Try to parse state from comma-separated address parts
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) < 2:
        return ""

    # For US addresses: "123 Main St, City, ST 12345" or "City, ST 12345, USA"
    # For other countries: "Street, City, Province, Country"
    # Work backwards — state is usually 2nd or 3rd from the end
    for part in reversed(parts[1:]):  # skip first part (street)
        # Remove ZIP/postal codes (digits at start or end)
        cleaned = re.sub(r'\b\d{4,10}\b', '', part).strip()
        cleaned = re.sub(r'^\d+\s*', '', cleaned).strip()
        cleaned = re.sub(r'\s*\d+$', '', cleaned).strip()
        if not cleaned:
            continue
        # Skip if it's just the country name
        if cleaned.lower() in [country.lower(), "usa", "us", "uk", "united states", "united kingdom"]:
            continue
        # Skip if it looks like a street address (has numbers)
        if re.match(r'^\d', part.strip()):
            continue
        # If it's a short code (2-3 letters like "CA", "NY", "TX") — likely a state
        if re.match(r'^[A-Z]{2,3}$', cleaned):
            return cleaned
        # If the cleaned part is a plausible state/province name (2-30 chars, no digits)
        if 2 <= len(cleaned) <= 30 and not re.search(r'\d', cleaned):
            return cleaned

    return ""


def clean_lead(raw: dict, source: str, keyword: str = "", country: str = "") -> Optional[dict]:
    domain = extract_domain(raw.get("website") or raw.get("domain") or "")
    if not domain or len(domain) < 4:
        return None

    company = (raw.get("company_name") or raw.get("name") or "").strip()
    if not company:
        return None

    lead_type = classify_lead_type(company, raw.get("description", ""))

    # Extract state from address if not provided directly
    state = raw.get("state") or ""
    if not state and raw.get("address"):
        state = extract_state_from_address(raw.get("address", ""), country)

    return {
        "company_domain": domain,
        "company_name": company,
        "website_url": raw.get("website") or f"https://{domain}",
        "contact_name": raw.get("contact_name") or "",
        "contact_email": raw.get("email") or "",
        "contact_phone": raw.get("phone") or "",
        "contact_title": raw.get("title") or "",
        "country": country or raw.get("country", "Unknown"),
        "city": raw.get("city") or "",
        "state": state,
        "lead_type": lead_type,
        "source": source,
        "keyword_used": keyword,
        "description": raw.get("description", ""),
        "has_contact_form": False,
        "score": 0,
        "sequence_stage": 0,
        "excluded": False,
        "email_sent": False,
        "form_filled": False,
        "form_submission_status": "pending",
    }


# Ordered: first match wins, so more specific seal/turned-part consumers come first.
LEAD_TYPE_RULES = [
    ("process_equipment_manufacturer", ["agitator", "mixer", "reactor", "process equipment", "pressure vessel", "heat exchanger"]),
    ("pump_valve_manufacturer", ["pump", "valve", "impeller"]),
    ("compressor_blower_manufacturer", ["compressor", "blower", "vacuum system"]),
    ("hydraulic_pneumatic_manufacturer", ["hydraulic", "pneumatic", "cylinder"]),
    ("automotive_component_manufacturer", ["automotive", "auto component", "auto parts", "tractor parts", "two wheeler"]),
    ("electrical_equipment_manufacturer", ["electric motor", "switchgear", "transformer", "electrical equipment", "motors"]),
    ("machinery_equipment_manufacturer", ["machinery", "machine manufacturer", "packaging machine", "special purpose machine",
                                          "conveyor", "gearbox", "gear box", "textile machine", "agricultural machine"]),
    ("general_engineering", ["engineering", "fabrication", "manufacturer", "manufacturing", "industries", "industrial"]),
]


def classify_lead_type(company_name: str, description: str = "") -> str:
    text = f"{company_name} {description}".lower()
    for lead_type, needles in LEAD_TYPE_RULES:
        if any(n in text for n in needles):
            return lead_type
    return "other"


# ═══════════════════════════════════════════════════════════════
# APIFY — GOOGLE MAPS SCRAPER (PRIMARY SOURCE)
# Actor: compass/crawler-google-places
# ═══════════════════════════════════════════════════════════════

APIFY_BASE = "https://api.apify.com/v2"


def _apify_available() -> bool:
    if not APIFY_API_KEY or "your" in APIFY_API_KEY.lower():
        return False
    return True


def scrape_google_maps_apify(keyword: str, country: str, city: str = "", max_places: int = 50) -> list[dict]:
    """
    Run compass/crawler-google-places on Apify and return cleaned leads.
    This is the PRIMARY scraping method.
    """
    if not _apify_available():
        logger.error("Apify API key not configured — cannot scrape Google Maps")
        return []

    location = f"{city}, {country}" if city else country
    search_query = f"{keyword} {location}"

    logger.info(f"[Apify Maps] Searching: '{search_query}' (max {max_places} places)")

    # Build the actor input for compass/crawler-google-places
    run_input = {
        "searchStringsArray": [search_query],
        "maxCrawledPlacesPerSearch": max_places,
        "language": "en",
        "deeperCityScrape": False,
    }

    url = f"{APIFY_BASE}/acts/compass~crawler-google-places/run-sync-get-dataset-items"
    params = {"token": APIFY_API_KEY}
    headers = {"Content-Type": "application/json"}

    try:
        resp = httpx.post(url, json=run_input, params=params, headers=headers, timeout=300)
        logger.info(f"[Apify Maps] Response status: {resp.status_code}")

        if resp.status_code == 401:
            logger.error("[Apify Maps] 401 Unauthorized — check your APIFY_API_KEY in .env")
            return []
        if resp.status_code == 400:
            logger.error(f"[Apify Maps] 400 Bad Request — {resp.text[:300]}")
            return []

        resp.raise_for_status()
        items = resp.json()

        if not isinstance(items, list):
            logger.warning(f"[Apify Maps] Unexpected response type: {type(items)}")
            logger.warning(f"[Apify Maps] Response preview: {str(items)[:500]}")
            return []

        logger.info(f"[Apify Maps] Got {len(items)} raw places from Apify")

        leads = []
        for place in items:
            # compass/crawler-google-places returns fields like:
            # title, website, phone, address, city, categoryName, url, etc.
            website = place.get("website") or ""
            if not website:
                # Skip places with no website — can't do outreach
                continue

            domain = extract_domain(website)
            if not domain:
                continue
            if any(s in domain for s in SKIP_DOMAINS):
                continue

            lead = {
                "company_name": place.get("title") or place.get("name") or "",
                "website": website,
                "domain": domain,
                "phone": place.get("phone") or place.get("phoneUnformatted") or "",
                "email": "",  # Google Maps doesn't provide email
                "country": country,
                "city": city or place.get("city") or place.get("neighborhood") or "",
                "state": place.get("state") or "",
                "address": place.get("address") or place.get("street") or "",
                "description": place.get("categoryName") or place.get("subTitle") or "",
            }

            cleaned = clean_lead(lead, source="google_maps", keyword=search_query, country=country)
            if cleaned:
                leads.append(cleaned)

        logger.info(f"[Apify Maps] Cleaned {len(leads)} valid leads from {len(items)} places")
        return leads

    except httpx.TimeoutException:
        logger.error(f"[Apify Maps] Timed out after 300s for '{search_query}'")
        return []
    except Exception as e:
        logger.error(f"[Apify Maps] Error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# APOLLO.IO (SECONDARY SOURCE — for contact details)
# ═══════════════════════════════════════════════════════════════

def scrape_apollo(country: str, page: int = 1, per_page: int = 100) -> list[dict]:
    if not APOLLO_API_KEY or "your" in APOLLO_API_KEY.lower():
        logger.info("Apollo API key not set, skipping")
        return []

    url = "https://api.apollo.io/v1/mixed_people/search"
    payload = {
        "api_key": APOLLO_API_KEY,
        "page": page, "per_page": per_page,
        "person_titles": APOLLO_JOB_TITLES,
        "person_locations": [country],
        "organization_num_employees_ranges": ["1,10000"],
        "q_keywords": "pump OR valve OR hydraulic cylinder OR compressor OR gearbox OR machinery OR auto components",
    }

    try:
        resp = httpx.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        leads = []
        for person in data.get("people", []):
            org = person.get("organization", {})
            lead = {
                "contact_name": f"{person.get('first_name', '')} {person.get('last_name', '')}".strip(),
                "email": person.get("email") or "",
                "phone": person.get("phone_number") or "",
                "title": person.get("title") or "",
                "company_name": org.get("name") or "",
                "website": org.get("website_url") or "",
                "domain": org.get("primary_domain") or "",
                "description": org.get("short_description") or "",
                "country": country,
                "city": person.get("city") or org.get("city") or "",
                "state": person.get("state") or org.get("state") or "",
            }
            cleaned = clean_lead(lead, source="apollo", keyword=f"apollo_{country}", country=country)
            if cleaned:
                leads.append(cleaned)

        logger.info(f"Apollo: {len(leads)} leads for {country}")
        return leads
    except Exception as e:
        logger.error(f"Apollo error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# FREE FALLBACKS (DuckDuckGo + Bing — no API key needed)
# ═══════════════════════════════════════════════════════════════

def _scrape_duckduckgo(keyword: str, country: str, num: int = 50) -> list[dict]:
    leads = []
    try:
        url = "https://html.duckduckgo.com/html/"
        data = {"q": keyword, "b": ""}
        resp = httpx.post(url, data=data, headers=HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True)
        soup = BeautifulSoup(resp.text, "lxml")

        for result in soup.select("div.result, div.web-result"):
            link_el = result.select_one("a.result__a, a.result__url, a[href]")
            title_el = result.select_one("a.result__a, h2 a, h3")
            snippet_el = result.select_one("a.result__snippet, .result__snippet, .snippet")
            if not link_el:
                continue

            href = link_el.get("href", "")
            if "uddg=" in href:
                import urllib.parse
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                href = parsed.get("uddg", [href])[0]

            domain = extract_domain(href)
            if not domain or any(s in domain for s in SKIP_DOMAINS):
                continue

            lead = {
                "company_name": title_el.get_text(strip=True) if title_el else domain,
                "website": href,
                "domain": domain,
                "description": snippet_el.get_text(strip=True) if snippet_el else "",
                "country": country,
            }
            cleaned = clean_lead(lead, source="google_search", keyword=keyword, country=country)
            if cleaned:
                leads.append(cleaned)
            if len(leads) >= num:
                break

        logger.info(f"DuckDuckGo: {len(leads)} leads for '{keyword}'")
    except Exception as e:
        logger.error(f"DuckDuckGo error: {e}")

    time.sleep(SCRAPE_DELAY_SECONDS)
    return leads


def _scrape_bing(keyword: str, country: str, num: int = 50) -> list[dict]:
    leads = []
    try:
        query = quote_plus(keyword)
        url = f"https://www.bing.com/search?q={query}&count={min(num, 50)}"
        resp = httpx.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True)
        soup = BeautifulSoup(resp.text, "lxml")

        for result in soup.select("li.b_algo"):
            link_el = result.select_one("h2 a")
            snippet_el = result.select_one("p, .b_caption p")
            if not link_el:
                continue

            href = link_el.get("href", "")
            domain = extract_domain(href)
            if not domain or any(s in domain for s in SKIP_DOMAINS):
                continue

            lead = {
                "company_name": link_el.get_text(strip=True),
                "website": href,
                "domain": domain,
                "description": snippet_el.get_text(strip=True) if snippet_el else "",
                "country": country,
            }
            cleaned = clean_lead(lead, source="google_search", keyword=keyword, country=country)
            if cleaned:
                leads.append(cleaned)
            if len(leads) >= num:
                break

        logger.info(f"Bing: {len(leads)} leads for '{keyword}'")
    except Exception as e:
        logger.error(f"Bing error: {e}")

    time.sleep(SCRAPE_DELAY_SECONDS)
    return leads


def scrape_google_search(keyword: str, country: str, num_results: int = 50) -> list[dict]:
    """Search fallback — DuckDuckGo then Bing."""
    leads = _scrape_duckduckgo(keyword, country, num_results)
    if leads:
        return leads
    return _scrape_bing(keyword, country, num_results)


# ═══════════════════════════════════════════════════════════════
# MASTER SCRAPE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════

def run_daily_scrape() -> list[dict]:
    """
    Extract up to DAILY_LEAD_TARGET quality-gated leads using the preserved keyword set.
    PRIMARY: Apify Google Maps (compass/crawler-google-places)
    SECONDARY: Apollo.io, DuckDuckGo/Bing search
    Leads are inserted into Supabase immediately as they pass the quality gate.
    """
    all_leads = []
    inserted_leads: list[dict] = []
    total_inserted = 0
    total_skipped = 0
    import config
    target = int(getattr(config, "DAILY_LEAD_TARGET", 50) or 50)

    from modules.database import get_leads_added_in_business_day
    already_today = get_leads_added_in_business_day(reset_hour_local=getattr(config, "BUSINESS_DAY_RESET_HOUR", 11))
    if already_today >= target:
        logger.info(f"=== Skipping scrape: already have {already_today}/{target} leads for current day ===")
        return []

    remaining_target = max(0, target - already_today)
    logger.info(f"=== Starting daily scrape — Target: {remaining_target} leads (already today: {already_today}/{target}) ===")
    logger.info(f"Apify available: {_apify_available()}")
    logger.info(f"Apollo available: {bool(APOLLO_API_KEY and 'your' not in APOLLO_API_KEY.lower())}")

    for country in list(getattr(config, "TARGET_COUNTRIES", []) or []):
        import importlib
        importlib.reload(config)
        current_countries = list(getattr(config, "TARGET_COUNTRIES", []) or [])
        if country not in current_countries:
            logger.info(f"Skipping {country} as it was removed from config during scrape")
            continue
        search_keywords = list(getattr(config, "SEARCH_KEYWORDS", []) or [])

        if total_inserted >= remaining_target:
            break

        if is_segment_paused("country", country):
            logger.info(f"Skipping paused country: {country}")
            continue

        remaining = remaining_target - total_inserted
        country_leads = []
        # Raw candidates are gathered generously because most fail the quality gate.
        candidate_goal = remaining * 4

        # ── PRIMARY: Google Maps via Apify ──────────────────
        if _apify_available():
            for keyword in search_keywords:
                if len(country_leads) >= candidate_goal:
                    break
                query = config.normalize_keyword(keyword)
                logger.info(f"[{country}] Apify Maps: '{query}'")
                maps_leads = scrape_google_maps_apify(query, country, max_places=30)
                country_leads.extend(maps_leads)
                update_source_tracker("google_maps", f"{query} {country}", country, new_found=len(maps_leads))
                time.sleep(SCRAPE_DELAY_SECONDS)

        # ── SECONDARY: Apollo.io ────────────────────────────
        if len(country_leads) < candidate_goal:
            apollo_leads = scrape_apollo(country)
            country_leads.extend(apollo_leads)
            update_source_tracker("apollo", f"apollo_{country}", country, new_found=len(apollo_leads))

        # ── TERTIARY: DuckDuckGo/Bing search ────────────────
        if len(country_leads) < candidate_goal:
            for keyword in search_keywords:
                if len(country_leads) >= candidate_goal:
                    break
                query = f"{config.normalize_keyword(keyword)} {country}"
                search_leads = scrape_google_search(query, country, num_results=20)
                country_leads.extend(search_leads)
                update_source_tracker("google_search", query, country, new_found=len(search_leads))
                time.sleep(SCRAPE_DELAY_SECONDS)

        # ── ENRICH + QUALITY GATE + INSERT ──────────────────
        if country_leads and total_inserted < remaining_target:
            seen = set()
            unique_batch = []
            for lead in country_leads:
                d = lead["company_domain"]
                if d not in seen:
                    seen.add(d)
                    unique_batch.append(lead)

            from modules.enricher import enrich_leads
            logger.info(f"[{country}] Quality-checking {len(unique_batch)} candidates (need {remaining})...")
            enriched_batch = enrich_leads(unique_batch, insert_immediately=True, max_keep=remaining) or []
            if enriched_batch:
                inserted_leads.extend(enriched_batch)
            total_inserted += len(enriched_batch)
            logger.info(f"[{country}] ✓ {len(enriched_batch)} leads saved to DB — running total: {total_inserted}/{remaining_target}")
            flag = get_country_flag(country)
            emit_log(
                f"{flag} {country} — Found {len(enriched_batch)} leads",
                level="info",
                category="lead",
                data={
                    "type": "leads_found",
                    "country": country,
                    "flag": flag,
                    "count": len(enriched_batch),
                    "total_inserted": total_inserted,
                    "source": "Apify + Apollo" if _apify_available() else "Apollo + Search",
                },
            )
        else:
            logger.info(f"[{country}] No leads found from any source")
            emit_log(
                f"{get_country_flag(country)} {country} — No leads found",
                level="warning",
                category="lead",
                data={"type": "no_leads", "country": country},
            )

        all_leads.extend(country_leads)
        logger.info(f"=== {country}: {len(country_leads)} leads (running total: {len(all_leads)}) ===")

    logger.info(f"=== Daily scrape done: {total_inserted} inserted, {total_skipped} skipped, {len(all_leads)} total scraped ===")
    return inserted_leads[:remaining_target]
