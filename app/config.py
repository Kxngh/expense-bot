"""Settings, read from environment variables (or a .env file)."""
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [x.strip().lstrip("+") for x in raw.split(",") if x.strip()]


# --- WhatsApp Cloud API (Meta) ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "change-me")
APP_SECRET = os.getenv("APP_SECRET", "")  # used to verify webhook signatures
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v26.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Only these WhatsApp numbers (country code, no +) may use the bot.
# Leave empty to allow anyone (not recommended).
ALLOWED_NUMBERS = _list("ALLOWED_NUMBERS")

# --- LLM parsing ---
# LLM_PROVIDER: "claude", "gemini" or "none" (keyword parser only).
# If unset, it picks whichever API key is present.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").lower() or (
    "claude" if ANTHROPIC_API_KEY else "gemini" if GEMINI_API_KEY else "none"
)
# Receipt photos are read only when this is on. Defaults to on for Claude and off
# for Gemini, since Gemini's free tier may use inputs for training / human review.
PARSE_IMAGES = os.getenv("PARSE_IMAGES", "on" if LLM_PROVIDER == "claude" else "off").lower() in ("1", "on", "true", "yes")

# --- Automatic reports ---
WEEKLY_REPORT_DAY = int(os.getenv("WEEKLY_REPORT_DAY", "6"))      # 0=Mon ... 6=Sun
WEEKLY_REPORT_HOUR = int(os.getenv("WEEKLY_REPORT_HOUR", "20"))   # 8 PM
MONTHLY_REPORT_HOUR = int(os.getenv("MONTHLY_REPORT_HOUR", "9"))  # 9 AM on the 1st
# Optional: name of an approved WhatsApp template (no variables) sent when a report is
# ready but the user hasn't messaged in 24h. Costs a small fee per message. Leave empty
# to hold the report until their next message instead (free).
REPORT_TEMPLATE = os.getenv("REPORT_TEMPLATE", "")
REPORT_TEMPLATE_LANG = os.getenv("REPORT_TEMPLATE_LANG", "en")
# Secret for GET /tasks/reports?key=..., called by an external cron (cron-job.org) at report
# time so a sleeping free-tier server wakes up and sends reports on schedule.
CRON_SECRET = os.getenv("CRON_SECRET", "")

# Shown in the welcome message's privacy note ("stored on Krishna's server")
BOT_OWNER_NAME = os.getenv("BOT_OWNER_NAME", "the bot owner")

# Webhooks must be signed by Meta (APP_SECRET). Only set this to "on" for local testing.
ALLOW_UNSIGNED_WEBHOOKS = os.getenv("ALLOW_UNSIGNED_WEBHOOKS", "off").lower() in ("1", "on", "true", "yes")

# --- App ---
DB_PATH = os.getenv("DB_PATH", "expenses.db")
# Set to a Postgres URL (e.g. Neon's) to use Postgres instead of the SQLite file above
DATABASE_URL = os.getenv("DATABASE_URL", "")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
CURRENCY = os.getenv("CURRENCY_SYMBOL", "₹")
