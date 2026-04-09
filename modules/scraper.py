"""
FlowLockHunter v2 — Lead Scraping Module
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
    SEARCH_KEYWORDS, TARGET_COUNTRIES,
    SCRAPE_DELAY_SECONDS, REQUEST_TIMEOUT, MAX_RETRIES,
    DAILY_LEAD_TARGET,
)
from modules.database import (
    bulk_check_domains, update_source_tracker, is_segment_paused,
)
from modules.events import emit_log, get_country_flag

logger = logging.getLogger("flowlockhunter.scraper")

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
        "has_contact_form": False,
        "score": 0,
    }


def classify_lead_type(company_name: str, description: str = "") -> str:
    text = (company_name + " " + description).lower()
    if any(k in text for k in ["chemical", "petrochemical", "refinery", "chemical plant"]):
        return "chemical_plant"
    if any(k in text for k in ["pharma", "pharmaceutical", "drug", "biotech"]):
        return "pharmaceutical"
    if any(k in text for k in ["oil", "gas", "petroleum", "lng", "upstream", "downstream"]):
        return "oil_gas"
    if any(k in text for k in ["water treatment", "desalination", "wastewater", "water utility"]):
        return "water_treatment"
    if any(k in text for k in ["power plant", "power generation", "energy", "thermal", "turbine"]):
        return "power_generation"
    if any(k in text for k in ["pump", "oem", "pump manufacturer", "impeller", "centrifugal"]):
        return "oem_pump_manufacturer"
    if any(k in text for k in ["food processing", "beverage", "dairy", "brewery", "food grade"]):
        return "food_processing"
    if any(k in text for k in ["engineering", "machining", "cnc", "precision", "fabrication"]):
        return "general_engineering"
    if any(k in text for k in ["manufacturing", "industrial", "plant", "factory"]):
        return "chemical_plant"
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
        "q_keywords": "mechanical seal OR CNC machining OR precision components OR industrial pump OR rotating equipment",
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

# Industrial/mechanical seal search queries for Google Maps
MAPS_SEARCH_QUERIES = [
    "mechanical seal manufacturer",
    "CNC precision machining company",
    "industrial pump manufacturer",
    "chemical plant",
    "pharmaceutical manufacturing plant",
    "oil and gas refinery",
    "water treatment plant",
    "power generation company",
    "food processing plant",
    "rotating equipment manufacturer",
]


def run_daily_scrape() -> list[dict]:
    """
    Run the full scraping pipeline.
    PRIMARY: Apify Google Maps (compass/crawler-google-places)
    SECONDARY: Apollo.io, DuckDuckGo/Bing search
    Leads are inserted into Supabase immediately per-country.
    """
    from modules.database import bulk_insert_leads

    all_leads = []
    total_inserted = 0
    total_skipped = 0
    target = DAILY_LEAD_TARGET

    logger.info(f"=== Starting daily scrape — target: {target} leads ===")
    logger.info(f"Apify available: {_apify_available()}")
    logger.info(f"Apollo available: {bool(APOLLO_API_KEY and 'your' not in APOLLO_API_KEY.lower())}")

    for country in TARGET_COUNTRIES:
        if len(all_leads) >= target:
            break

        if is_segment_paused("country", country):
            logger.info(f"Skipping paused country: {country}")
            continue

        remaining = target - len(all_leads)
        country_leads = []

        # ── PRIMARY: Google Maps via Apify ──────────────────
        if _apify_available():
            for query in MAPS_SEARCH_QUERIES:
                if len(country_leads) >= remaining:
                    break

                logger.info(f"[{country}] Apify Maps: '{query}'")
                maps_leads = scrape_google_maps_apify(query, country, max_places=30)
                country_leads.extend(maps_leads)
                update_source_tracker("google_maps", f"{query} {country}", country, new_found=len(maps_leads))

                if maps_leads:
                    logger.info(f"[{country}] Got {len(maps_leads)} leads from '{query}'")

                time.sleep(SCRAPE_DELAY_SECONDS)

        # ── SECONDARY: Apollo.io ────────────────────────────
        if len(country_leads) < remaining:
            apollo_leads = scrape_apollo(country)
            country_leads.extend(apollo_leads)
            update_source_tracker("apollo", f"apollo_{country}", country, new_found=len(apollo_leads))

        # ── TERTIARY: DuckDuckGo/Bing search ────────────────
        if len(country_leads) < remaining:
            for kw_template in SEARCH_KEYWORDS[:3]:  # limit to first 3 keywords
                if len(country_leads) >= remaining:
                    break
                keyword = kw_template.format(country=country)
                search_leads = scrape_google_search(keyword, country, num_results=20)
                country_leads.extend(search_leads)
                update_source_tracker("google_search", keyword, country, new_found=len(search_leads))
                time.sleep(SCRAPE_DELAY_SECONDS)

        # ── ENRICH + INSERT INTO SUPABASE ───────────────────
        if country_leads:
            # Deduplicate within this batch
            seen = set()
            unique_batch = []
            for lead in country_leads:
                d = lead["company_domain"]
                if d not in seen:
                    seen.add(d)
                    unique_batch.append(lead)

            # Enrich: fetch website → extract email & phone → score → insert into DB
            # Each lead is inserted immediately after enrichment (no batching)
            # Only leads with email or phone from the website are kept
            from modules.enricher import enrich_leads
            logger.info(f"[{country}] Enriching {len(unique_batch)} leads (fetching websites + inserting)...")
            enriched_batch = enrich_leads(unique_batch, insert_immediately=True)
            total_inserted += len(enriched_batch)
            logger.info(f"[{country}] ✓ {len(enriched_batch)} leads saved to DB — running total: {total_inserted}")
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
    return all_leads[:target]
