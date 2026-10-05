"""Turns one inbound WhatsApp message into replies."""
import logging
from datetime import date

from . import commands, config, db, onboarding, parser, query, settings
from .charts import money

log = logging.getLogger(__name__)


def _budget_notes(wa_id: str, cats: set[str], added: dict[str, float]) -> list[str]:
    """Budget status for categories just touched; alert when a threshold is crossed."""
    budgets = db.get_budgets(wa_id)
    if not budgets:
        return []
    start, end = commands.month_bounds()
    totals = db.totals_by_category(wa_id, start, end)
    notes = []

    def check(label, now, before, budget):
        if before <= budget < now:
            notes.append(f"🚨 {label} budget exceeded: {money(now)} of {money(budget)}")
        elif before < 0.8 * budget <= now:
            notes.append(f"⚠️ {label} at {now / budget:.0%} of budget ({money(budget - now)} left)")
        else:
            notes.append(f"{label}: {money(now)} / {money(budget)} this month")

    for cat in sorted(cats):
        if cat in budgets:
            now = totals.get(cat, 0)
            check(cat, now, now - added.get(cat, 0), budgets[cat])
    if db.TOTAL in budgets:
        now = sum(totals.values())
        check("Total", now, now - sum(added.values()), budgets[db.TOTAL])
    return notes


def log_expenses(wa_id: str, result: parser.ParseResult, source: str) -> list:
    month_start, month_end = commands.month_bounds()
    lines, cats, added = [], set(), {}
    for e in result.expenses:
        when = e.date
        try:
            if when:
                date.fromisoformat(when)
        except ValueError:
            when = None
        exp_id = db.add_expense(wa_id, e.amount, e.category, e.description, e.merchant, when, source)
        d = date.fromisoformat(when) if when else db.today()
        tag = "" if d == db.today() else f" · {d.strftime('%d %b')}"
        lines.append(f"✅ {money(e.amount)} · {e.category} · {e.description}{tag}  #{exp_id}")
        if month_start <= d < month_end:
            cats.add(e.category)
            added[e.category] = added.get(e.category, 0) + e.amount
    if len(result.expenses) > 1:
        lines.append(f"Total {money(sum(e.amount for e in result.expenses))}")
    notes = _budget_notes(wa_id, cats, added)
    if notes:
        lines.append("")
        lines += notes
    lines.append("_Wrong? /undo or /fix <#> <category>_")
    return [("text", "\n".join(lines))]


async def handle(wa_id: str, msg: dict, wa) -> list:
    """msg is one entry from the webhook's `messages` array. `wa` is the WhatsApp client."""
    mtype = msg.get("type")
    if mtype == "text":
        text = msg["text"]["body"].strip()
        replies = commands.run(wa_id, text)
        if replies is not None:
            return replies
        result = await parser.parse(text=text, wa_id=wa_id)
        source = "text"
    elif mtype in ("image", "document"):
        media = msg[mtype]
        mime = media.get("mime_type", "")
        if not config.PARSE_IMAGES:
            caption = media.get("caption", "").strip()
            if caption:  # "1834 dmart" sent as a photo caption still gets logged
                result = await parser.parse(text=caption, wa_id=wa_id)
                if result.intent == "log":
                    return log_expenses(wa_id, result, "text")
            return [("text", "Photos aren't turned on. Type it instead, like *1834 dmart*.")]
        if not mime.startswith("image/"):
            return [("text", "I can read photos of bills/UPI screenshots. For PDFs, type the amount instead.")]
        data, mime = await wa.download_media(media["id"])
        result = await parser.parse(text=media.get("caption", ""), image=data, image_mime=mime, wa_id=wa_id)
        source = "image"
    else:
        return [("text", "Send an expense like *220 swiggy*, a bill photo, or /help.")]

    if result.intent == "log":
        return log_expenses(wa_id, result, source)
    if result.intent == "settings" and result.settings:
        return settings.apply(wa_id, result.settings)
    if result.intent == "query" and result.query:
        return query.answer(wa_id, result.query)
    if result.intent == "command" and result.command:
        replies = commands.run(wa_id, result.command if result.command.startswith("/") else "/" + result.command)
        if replies:
            return replies
    return [("text", result.reply or "Send an expense like *220 swiggy* or type /help.")]


async def handle_inbound(wa_id: str, msg: dict, wa, profile_name: str | None = None) -> list:
    """Entry point for every message: onboarding and account actions first, then the normal flow."""
    text = msg["text"]["body"].strip() if msg.get("type") == "text" else None
    gate_replies, pending = await onboarding.gate(wa_id, text, profile_name)
    if gate_replies is not None:
        if pending:  # the expense they sent before saying YES
            gate_replies += await handle(wa_id, {"type": "text", "text": {"body": pending}}, wa)
        return gate_replies

    user = db.get_user(wa_id)
    norm = (text or "").strip().lower()
    if user and user["pending_action"] == "delete":
        db.update_user(wa_id, pending_action=None)
        if norm == "delete":
            n = db.delete_user_data(wa_id)
            return [("text", f"🗑️ Deleted everything: {n} expenses, your budgets, categories and reports. "
                             "Message me anytime to start fresh.")]
        if norm in ("no", "cancel", "stop"):
            return [("text", "Cancelled - nothing was deleted.")]
    if norm in ("/deletemydata", "/deleteme", "/deleteaccount"):
        db.update_user(wa_id, pending_action="delete")
        n = len(db.recent(wa_id, 10**9))
        return [("text", f"⚠️ This permanently deletes all your data: {n} expenses, budgets, categories and "
                         "reports. It can't be undone.\n\nReply *DELETE* to confirm, or anything else to cancel.")]
    return await handle(wa_id, msg, wa)


async def deliver(wa, to: str, replies: list):
    for r in replies:
        kind = r[0]
        if kind == "text":
            await wa.send_text(to, r[1])
        elif kind == "image":
            await wa.send_image(to, r[1], r[2])
        elif kind == "document":
            await wa.send_document(to, r[1], r[2], r[3], r[4])
