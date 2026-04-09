"""
FlowLockHunter v2 — Event System
Callback-based event emitter for structured dashboard logging.
Avoids circular imports between modules and server.py.
"""

import logging

logger = logging.getLogger("flowlockhunter.events")

# ═══════════════════════════════════════════════════════════════
# CALLBACK REGISTRY
# ═══════════════════════════════════════════════════════════════

_log_callback = None


def set_log_callback(fn):
    """Register the server's add_log function as the callback."""
    global _log_callback
    _log_callback = fn


def emit_log(msg: str, level: str = "info", category: str = "system", data: dict | None = None):
    """Emit a structured log event to the dashboard."""
    if _log_callback:
        _log_callback(msg, level, category, data)
    else:
        logger.info(f"[{category}] {msg}")


# ═══════════════════════════════════════════════════════════════
# COUNTRY FLAGS
# ═══════════════════════════════════════════════════════════════

COUNTRY_FLAGS = {
    "UAE": "\U0001f1e6\U0001f1ea",
    "UK": "\U0001f1ec\U0001f1e7",
    "US": "\U0001f1fa\U0001f1f8",
    "India": "\U0001f1ee\U0001f1f3",
    "Germany": "\U0001f1e9\U0001f1ea",
    "France": "\U0001f1eb\U0001f1f7",
    "Netherlands": "\U0001f1f3\U0001f1f1",
    "South Africa": "\U0001f1ff\U0001f1e6",
    "Nigeria": "\U0001f1f3\U0001f1ec",
    "Kenya": "\U0001f1f0\U0001f1ea",
    "Saudi Arabia": "\U0001f1f8\U0001f1e6",
    "Singapore": "\U0001f1f8\U0001f1ec",
    "Malaysia": "\U0001f1f2\U0001f1fe",
    "Philippines": "\U0001f1f5\U0001f1ed",
    "Bangladesh": "\U0001f1e7\U0001f1e9",
    "Pakistan": "\U0001f1f5\U0001f1f0",
    "Turkey": "\U0001f1f9\U0001f1f7",
    "Egypt": "\U0001f1ea\U0001f1ec",
    "Ghana": "\U0001f1ec\U0001f1ed",
    "Tanzania": "\U0001f1f9\U0001f1ff",
    "Brazil": "\U0001f1e7\U0001f1f7",
    "Mexico": "\U0001f1f2\U0001f1fd",
    "Colombia": "\U0001f1e8\U0001f1f4",
}


def get_country_flag(country: str) -> str:
    """Get emoji flag for a country name."""
    return COUNTRY_FLAGS.get(country, "")
