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
    intent: str  # "log" | "query" | "settings" | "edit" | "command" | "chat"
    expenses: list[ParsedExpense] = field(default_factory=list)
    command: str = ""   # e.g. "/budget", for questions a built-in command answers
    reply: str = ""     # short reply for small talk / unclear input
    query: dict = field(default_factory=dict)  # structured filter for intent=query
    settings: dict = field(default_factory=dict)  # budgets / categories for intent=settings
    edit: dict = field(default_factory=dict)  # changes to an existing entry for intent=edit


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
                "enum": ["log", "edit", "query", "settings", "command", "chat"],
                "description": "log = the message contains one or more NEW expenses to save. "
                               "edit = the user is correcting an entry already logged (wrong amount, "
                               "category, description or date), e.g. 'no its 4000 not 400', 'make #8 500', "
                               "'that was yesterday'. "
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
            "edit": {
                "type": "object",
                "description": "For intent=edit: which entry to change and only the fields that change.",
                "properties": {
                    "id": {"type": "integer", "description": "Entry number (#). Use the latest entry if the user doesn't say."},
                    "amount": {"type": "number", "description": "New amount in rupees"},
                    "category": {"type": "string", "enum": cats},
                    "description": {"type": "string", "description": "New 2-5 word description"},
                    "date": {"type": "string", "description": "New date, YYYY-MM-DD"},
                },
                "required": ["id"],
            },
            "command": {
                "type": "string",
                "description": "For intent=command: '/budget', '/trends', '/daily', '/recent', '/help', "
                               "'/undo' or '/delete <#>' to remove an entry, "
                               "or for a full summary/report: '/report week', '/report lastweek', '/report month', '/report lastmonth'.",
            },
            "reply": {"type": "string", "description": "For intent=chat, a one-line friendly reply."},
        },
        "required": ["intent"],
    },
    }


TOOL = build_tool(categories.CATEGORIES)


def _recent_context(wa_id: str | None) -> str:
    rows = db.recent_logged(wa_id, 5) if wa_id else []
    if not rows:
        return ""
    items = "; ".join(f"#{r['id']} {r['spent_on']} {r['amount']:g} {r['description']} ({r['category']})" for r in rows)
    return f" The user's latest entries, newest first: {items}."


def _system_prompt(wa_id: str | None = None) -> str:
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
        "pick the closest existing category (including the user's own ones). "
        "Corrections to something already logged ('oh no its 4000 not 400', 'change that to food', "
        "'#8 was yesterday') -> intent=edit on that entry (the latest one unless they say which); "
        "never log it again as a new expense. Only the app changes data: never reply as if you "
        "updated, deleted or saved anything when using intent=chat."
        + _recent_context(wa_id)
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
            data = await _claude(text, image, image_mime, tool, wa_id)
        elif provider == "gemini" and config.GEMINI_API_KEY:
            data = await _gemini(text, image, image_mime, tool, wa_id)
        else:
            return fallback_parse(text, wa_id)
    except Exception:
        log.exception("%s parse failed, using fallback", provider)
        if text:
            return fallback_parse(text, wa_id)
        return ParseResult("chat", reply="Couldn't read that image right now - type the amount instead?")
    return _from_tool(data, cats)


async def _claude(text, image, image_mime, tool=TOOL, wa_id=None) -> dict:
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
        system=_system_prompt(wa_id),
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


async def _gemini(text, image, image_mime, tool=TOOL, wa_id=None) -> dict:
    """Same JSON schema as the Claude tool, via generateContent's structured output."""
    import json

    parts = []
    if image:
        parts.append({"inline_data": {"mime_type": image_mime, "data": base64.b64encode(image).decode()}})
    parts.append({"text": text or "(receipt photo, no caption)"})
    body = {
        "system_instruction": {"parts": [{"text": _system_prompt(wa_id)}]},
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
    edit = data.get("edit") if isinstance(data.get("edit"), dict) else {}
    if edit.get("category") and edit["category"] not in cats:
        edit.pop("category")
    if intent == "edit" and not any(edit.get(k) for k in ("amount", "category", "description", "date")):
        intent = "chat"
    return ParseResult(intent, exps, data.get("command", ""), data.get("reply", ""),
                       data.get("query") or {}, data.get("settings") or {}, edit)


# ---------------- offline fallback ----------------
_AMOUNT = re.compile(r"(?:rs\.?|inr|₹)?\s*(\d+(?:[.,]\d+)?)\s*(k)?\b", re.I)
_NUM = r"(?:rs\.?|₹)?\s*(\d+(?:\.\d+)?k?)"
_CORRECTION = re.compile(_NUM + r"\s*,?\s+not\s+" + _NUM + r"\b", re.I)


def _to_amount(s: str) -> float:
    s = s.lower()
    return float(s[:-1]) * 1000 if s.endswith("k") else float(s)


def fallback_parse(text: str, wa_id: str | None = None) -> ParseResult:
    """Handles '220 swiggy', 'swiggy 220', 'chai 20, auto 80', '1.2k shoes' and '4000 not 400'."""
    m = _CORRECTION.search(text or "")
    if m:  # "its 4000 not 400": fix the recent entry that has the wrong amount
        new, old = _to_amount(m.group(1)), _to_amount(m.group(2))
        rows = db.recent_logged(wa_id, 5) if wa_id else []
        match = next((r for r in rows if abs(r["amount"] - old) < 0.01), None)
        return ParseResult("edit", edit={"id": match["id"] if match else None, "amount": new})
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
