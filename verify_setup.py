#!/usr/bin/env python3
"""
MicrodyneHunter v2 — Setup Verification
Run this after install.bat to check everything is working.
"""

import sys

print()
print("=" * 56)
print("  MicrodyneHunter v2 — Setup Verification")
print("=" * 56)
print()

errors = []
warnings = []

# Check Python version
print(f"  Python: {sys.version.split()[0]}", end="")
if sys.version_info >= (3, 10):
    print(" ✅")
else:
    print(" ⚠️  (3.10+ recommended)")
    warnings.append("Python 3.10+ recommended")

# Check each dependency
deps = {
    "supabase": "Supabase (database)",
    "anthropic": "Anthropic (Claude AI)",
    "httpx": "HTTPX (HTTP client)",
    "dotenv": "python-dotenv (env vars)",
    "playwright": "Playwright (form filling)",
    "bs4": "BeautifulSoup (HTML parsing)",
    "lxml": "lxml (HTML parser)",
    "tenacity": "Tenacity (retry logic)",
    "schedule": "Schedule (cron jobs)",
    "rich": "Rich (terminal UI)",
    "pydantic": "Pydantic (data validation)",
}

print()
print("  Dependencies:")
for module, name in deps.items():
    try:
        __import__(module)
        print(f"    {name:35s} ✅")
    except ImportError:
        print(f"    {name:35s} ❌ NOT INSTALLED")
        errors.append(f"{name} is not installed")

# Check Playwright browsers
print()
print("  Playwright Browser:")
try:
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
        capture_output=True, text=True, timeout=10
    )
    # If chromium is installed, check for it
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        browser.close()
    print("    Chromium                            ✅")
except Exception as e:
    print(f"    Chromium                            ❌ Not installed")
    errors.append("Playwright Chromium not installed — run: python -m playwright install chromium")

# Check .env file
print()
print("  Configuration:")
import os
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    print(f"    .env file                           ✅")
    # Check if keys are filled
    from dotenv import load_dotenv
    load_dotenv(env_path)
    keys = {
        "SUPABASE_URL": "Supabase URL",
        "SUPABASE_KEY": "Supabase Key",
        "ANTHROPIC_API_KEY": "Anthropic API Key",
        "APOLLO_API_KEY": "Apollo API Key",
        "INSTANTLY_API_KEY": "Instantly API Key",
    }
    for key, name in keys.items():
        val = os.getenv(key, "")
        if val and val != f"your-{key.lower().replace('_','-')}":
            print(f"    {name:35s} ✅ Configured")
        else:
            print(f"    {name:35s} ⚠️  Not set yet")
            warnings.append(f"{name} not configured in .env")
else:
    print(f"    .env file                           ❌ Missing")
    errors.append(".env file not found — copy .env.example to .env")

# Check SQL schema
sql_path = os.path.join(os.path.dirname(__file__), "sql", "001_schema.sql")
if os.path.exists(sql_path):
    print(f"    SQL schema file                     ✅")
else:
    print(f"    SQL schema file                     ❌ Missing")
    errors.append("sql/001_schema.sql not found")

# Summary
print()
print("=" * 56)
if not errors:
    if warnings:
        print(f"  ⚠️  Setup OK with {len(warnings)} warning(s)")
        for w in warnings:
            print(f"     → {w}")
    else:
        print("  ✅ Everything looks good! Ready to run.")
    print()
    print("  Run the agent:")
    print("    python main.py --stats     (check database)")
    print("    python main.py --scrape    (scrape leads)")
    print("    python main.py             (full pipeline)")
else:
    print(f"  ❌ {len(errors)} error(s) found:")
    for e in errors:
        print(f"     → {e}")
    if warnings:
        print(f"  ⚠️  {len(warnings)} warning(s):")
        for w in warnings:
            print(f"     → {w}")
    print()
    print("  Fix the errors above, then run this script again.")

print("=" * 56)
print()
