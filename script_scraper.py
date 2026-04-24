import sys
content = open('modules/scraper.py', 'r', encoding='utf-8').read()

old_code = '''    for country in target_countries:
        if len(all_leads) >= target:
            break

        if is_segment_paused("country", country):
            logger.info(f"Skipping paused country: {country}")
            continue'''

new_code = '''    for country in list(getattr(config, "TARGET_COUNTRIES", []) or []):
        import importlib
        importlib.reload(config)
        current_countries = list(getattr(config, "TARGET_COUNTRIES", []) or [])
        if country not in current_countries:
            logger.info(f"Skipping {country} as it was removed from config during scrape")
            continue
        search_keywords = list(getattr(config, "SEARCH_KEYWORDS", []) or [])

        if len(all_leads) >= target:
            break

        if is_segment_paused("country", country):
            logger.info(f"Skipping paused country: {country}")
            continue'''

content = content.replace(old_code, new_code)
open('modules/scraper.py', 'w', encoding='utf-8').write(content)
