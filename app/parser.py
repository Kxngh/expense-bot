"""Turn a WhatsApp message (text or receipt photo) into structured expenses.

Uses Claude with a forced tool call so the output is always valid JSON.
Falls back to a simple keyword parser if no API key is set or the call fails.
"""
import base64
import logging
import re
from dataclasses import dataclass, field

from . import categories, config, db

log = logging.getLogger(__name__)


@dataclass
class ParsedExpense:
    amount: float
    category: str
    description: str = ""
    merchant: str = ""
    date: str | None = None  # YYYY-MM-DD


@dataclass
class ParseResult:
    intent: str  # "log" | "query" | "command" | "chat"
    expenses: list[ParsedExpense] = field(default_factory=list)
    command: str = ""   # e.g. "/budget", for questions a built-in command answers
    reply: str = ""     # short reply for small talk / unclear input
    query: dict = field(default_factory=dict)  # structured filter for intent=query
    settings: dict = field(default_factory=dict)  # budgets / categories for intent=settings


def build_tool(cats: list[str]) -> dict:
    """The output schema. `cats` is this user's own category list (built-ins + their custom ones)."""
    return {
    "name": "record",
    "description": "Record what the user's WhatsApp message means for their expense tracker.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["log", "query", "settings", "command", "chat"],
                "description": "log = the message contains one or more expenses to save. "
                               "settings = the user wants to set/change/remove a budget, or add/remove a category. "
                               "query = a question about amounts, counts, lists or comparisons of their spending. "
                               "command = a question about budgets, overall trends or spending pace "
                               "(map to /budget, /trends, /daily, /recent or /help). "
                               "chat = anything else (greetings, unclear text).",
            },
            "expenses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number", "description": "Amount in rupees. '1.2k' = 1200."},
                        "category": {"type": "string", "enum": cats},
                        "description": {"type": "string", "description": "2-5 word description, e.g. 'Lunch with team'"},
                        "merchant": {"type": "string", "description": "Shop/app/brand if known, else empty"},
                        "date": {"type": "string", "description": "YYYY-MM-DD. Resolve 'yesterday', 'last friday', etc."},
                    },
                    "required": ["amount", "category", "description", "date"],
                },
            },
            "query": {
                "type": "object",
                "description": "For intent=query: a filter the app runs on the user's expenses.",
                "properties": {
                    "start": {"type": "string", "description": "First day, YYYY-MM-DD (inclusive)"},
                    "end": {"type": "string", "description": "Last day, YYYY-MM-DD (inclusive). Not after today."},
                    "period_label": {"type": "string", "description": "How to describe the period, e.g. 'this month', 'last 3 days', 'in September'"},
                    "categories": {"type": "array", "items": {"type": "string", "enum": cats},
                                   "description": "Only these categories; empty = all"},
                    "search": {"type": "string", "description": "Merchant or keyword to match, e.g. 'swiggy', 'auto', 'netflix'. Empty if none."},
                    "metric": {"type": "string",
                               "enum": ["total", "count", "average_per_day", "average_per_expense", "largest", "list"],
                               "description": "total = sum; count = how many times; largest = biggest expenses; list = show entries"},
                    "group_by": {"type": "string", "enum": ["none", "category", "merchant", "day", "week", "month"],
                                 "description": "Break the result down by this; 'none' for a single answer"},
                    "compare_start": {"type": "string", "description": "For comparisons ('vs last month'): first day of the other period"},
                    "compare_end": {"type": "string", "description": "Last day of the other period"},
                    "compare_label": {"type": "string", "description": "e.g. 'in August', 'last week'"},
                    "limit": {"type": "integer", "description": "For largest/list: how many to show (default 5)"},
                },
                "required": ["start", "end", "period_label", "metric", "group_by"],
            },
            "settings": {
                "type": "object",
                "description": "For intent=settings.",
                "properties": {
                    "budgets": {
                        "type": "array",
                        "description": "Monthly budgets to set. amount 0 = remove that budget.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string", "enum": [*cats, "TOTAL"],
                                             "description": "TOTAL = overall monthly budget"},
                                "amount": {"type": "number"},
                            },
                            "required": ["category", "amount"],
                        },
                    },
                    "add_categories": {"type": "array", "items": {"type": "string"},
                                       "description": "New category names, short and title-cased, e.g. 'Pets', 'Gym'"},
                    "remove_categories": {"type": "array", "items": {"type": "string"},
                                          "description": "Custom categories to delete"},
                },
            },
            "command": {
                "type": "string",
                "description": "For intent=command: '/budget', '/trends', '/daily', '/recent', '/help', "
                               "or for a full summary/report: '/report week', '/report lastweek', '/report month', '/report lastmonth'.",
            },
            "reply": {"type": "string", "description": "For intent=chat, a one-line friendly reply."},
        },
        "required": ["intent"],
    },
    }


TOOL = build_tool(categories.CATEGORIES)


def _system_prompt() -> str:
    t = db.now()
    return (
        "You parse messages for a personal expense tracker used in India (amounts in INR). "
        f"Today is {t.strftime('%A, %d %B %Y')} ({t.date().isoformat()}). "
        "A message may list several expenses ('chai 20, auto 80, dinner 450') - return each one. "
        "For receipts/bills/UPI screenshots, record the final amount paid as ONE expense unless the "
        "user clearly wants items split. Default the date to today. Never invent an amount; if none "
        "is present, use intent=chat and ask for it. Food ordered from Swiggy/Zomato is Food & Dining; "
        "Blinkit/Zepto/Instamart is Groceries unless the items say otherwise. "
        "For questions, prefer intent=query and fill the filter precisely: resolve relative dates "
        "('this week' starts Monday, 'last month' is the full previous calendar month); when no period "
        "is mentioned use this month so far. Put brands/places/things (Swiggy, auto, chai) in search, "
        "and use categories only when the user names a category (food, transport). "
        "'how often'/'how many times' -> count; 'biggest'/'most expensive' -> largest; "
        "'where does my money go'/'breakdown' -> total grouped by category; 'X vs Y' -> fill compare_*. "
        "Budgets are monthly: 'my food budget is 6k', 'keep shopping under 3000', 'I want to spend 30k a month' "
        "-> intent=settings. Only create a category when the user explicitly asks for one; when logging, "
        "pick the closest existing category (including the user's own ones)."
    )


_client = None


def _get_client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def parse(text: str = "", image: bytes | None = None, image_mime: str = "image/jpeg",
                wa_id: str | None = None) -> ParseResult:
    provider = config.LLM_PROVIDER
    cats = categories.for_user(wa_id)
    tool = build_tool(cats)
    if image and not config.PARSE_IMAGES:
        return ParseResult("chat", reply="Photos aren't turned on. Type it instead, like *1834 dmart*.")
    try:
        if provider == "claude" and config.ANTHROPIC_API_KEY:
            data = await _claude(text, image, image_mime, tool)
        elif provider == "gemini" and config.GEMINI_API_KEY:
            data = await _gemini(text, image, image_mime, tool)
        else:
            return fallback_parse(text)
    except Exception:
        log.exception("%s parse failed, using fallback", provider)
        if text:
            return fallback_parse(text)
        return ParseResult("chat", reply="Couldn't read that image right now - type the amount instead?")
    return _from_tool(data, cats)


async def _claude(text, image, image_mime, tool=TOOL) -> dict:
    content = []
    if image:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image_mime, "data": base64.b64encode(image).decode()},
        })
    content.append({"type": "text", "text": text or "(receipt photo, no caption)"})

    resp = await _get_client().messages.create(
        model=config.LLM_MODEL,
        max_tokens=1024,
        system=_system_prompt(),
        tools=[tool],
        tool_choice={"type": "tool", "name": "record"},
        messages=[{"role": "user", "content": content}],
    )
    return next(b.input for b in resp.content if b.type == "tool_use")


# ---------------- Gemini (AI Studio key) ----------------
_http = None  # tests swap this for a mock transport


def _gemini_http():
    global _http
    if _http is None:
        import httpx
        _http = httpx.AsyncClient(timeout=30)
    return _http


async def _gemini(text, image, image_mime, tool=TOOL) -> dict:
    """Same JSON schema as the Claude tool, via generateContent's structured output."""
    import json

    parts = []
    if image:
        parts.append({"inline_data": {"mime_type": image_mime, "data": base64.b64encode(image).decode()}})
    parts.append({"text": text or "(receipt photo, no caption)"})
    body = {
        "system_instruction": {"parts": [{"text": _system_prompt()}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": tool["input_schema"],
            "temperature": 0,
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    r = await _gemini_http().post(url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY})
    if r.status_code == 429:
        log.warning("Gemini free-tier rate limit hit; falling back to keyword parser")
    r.raise_for_status()
    cand = r.json()["candidates"][0]
    out = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
    return json.loads(out)


def _from_tool(data: dict, cats: list[str] | None = None) -> ParseResult:
    cats = cats or categories.CATEGORIES
    intent = data.get("intent", "chat")
    exps = []
    for e in data.get("expenses") or []:
        try:
            amt = float(e["amount"])
        except (KeyError, TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        cat = e.get("category") if e.get("category") in cats else "Other"
        exps.append(ParsedExpense(amt, cat, e.get("description", ""), e.get("merchant", ""), e.get("date")))
    if intent == "log" and not exps:
        intent = "chat"
    if intent == "query" and not isinstance(data.get("query"), dict):
        intent = "chat"
    if intent == "settings" and not isinstance(data.get("settings"), dict):
        intent = "chat"
    return ParseResult(intent, exps, data.get("command", ""), data.get("reply", ""),
                       data.get("query") or {}, data.get("settings") or {})


# ---------------- offline fallback ----------------
_AMOUNT = re.compile(r"(?:rs\.?|inr|₹)?\s*(\d+(?:[.,]\d+)?)\s*(k)?\b", re.I)


def fallback_parse(text: str) -> ParseResult:
    """Handles '220 swiggy', 'swiggy 220', 'chai 20, auto 80' and '1.2k shoes'."""
    items = [p.strip() for p in re.split(r"[,\n;]+", text or "") if p.strip()]
    exps = []
    for item in items:
        m = _AMOUNT.search(item)
        if not m:
            continue
        amt = float(m.group(1).replace(",", "."))
        if m.group(2):
            amt *= 1000
        desc = (item[: m.start()] + item[m.end():]).strip(" -:") or "Expense"
        exps.append(ParsedExpense(amt, categories.guess(desc), desc.capitalize()))
    if exps:
        return ParseResult("log", exps)
    return ParseResult("chat", reply="Send an expense like *220 swiggy* or type /help.")
