"""Slash commands and summaries. Every handler returns a list of replies:
("text", str) | ("image", png_bytes, caption) | ("document", bytes, filename, mime, caption)
"""
import csv
import io
from calendar import monthrange
from datetime import date, timedelta

from . import categories, charts, db, reports
from .charts import money

HELP = """*Expense bot* 💸

*Log* - just type it:
• 220 swiggy
• chai 20, auto 80, dinner 450
• 1.2k shoes yesterday
• or send a photo of a bill / UPI screenshot

*See*
/today · /week · /month - summaries
/month 2026-09 - a past month
/cat food - one category this month
/trends - last 6 months
/daily - this month's spending pace
/recent - last 10 entries

*Reports*
Weekly (Sun evening) and monthly (1st) arrive automatically
/report week · /report month - this week/month so far
/report lastweek · /report lastmonth
/reports off - stop automatic reports

*Budgets & categories* - just say it
• my food budget is 6000
• I want to spend 30k a month
• add a category for Pets
/budget - see budgets & usage
/categories - see your categories
/budget total 30000 - monthly budget
/budget food 6000 - category budget
/budget food 0 - remove it

*Fix*
/undo - delete last entry
/delete 12 - delete entry #12
/fix 12 groceries - change category
/edit 12 4000 - change amount
• or just say "it was 4000 not 400"
/export - CSV of this month
/deletemydata - erase everything

*Ask anything*
• how much on swiggy this month?
• how many times did I take an auto last week?
• biggest expenses in september
• food this month vs last month
• where does my money go?"""


def month_bounds(ym: str | None = None) -> tuple[date, date]:
    if ym:
        y, m = map(int, ym.split("-"))
    else:
        t = db.today()
        y, m = t.year, t.month
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    return start, end


def _pct_line(name, spent, budget):
    pct = spent / budget
    icon = "🚨" if pct > 1 else "⚠️" if pct >= 0.8 else "🟢"
    return f"{icon} {name}: {money(spent)} of {money(budget)} ({pct:.0%})"


def _ym(arg: str) -> str | None:
    try:
        y, m = arg.split("-")
        if len(y) == 4 and 1 <= int(m) <= 12:
            return f"{int(y)}-{int(m):02d}"
    except ValueError:
        pass
    return None


# ---------------- summaries ----------------
def period_summary(wa_id, start: date, end: date, title: str) -> str:
    totals = db.totals_by_category(wa_id, start, end)
    spent = sum(totals.values())
    if not totals:
        return f"*{title}*\nNothing logged yet."
    lines = [f"*{title}* - {money(spent)}"]
    for cat, v in totals.items():
        lines.append(f"• {cat}: {money(v)} ({v / spent:.0%})")
    rows = db.between(wa_id, start, end)
    lines.append(f"\n{len(rows)} entries")
    return "\n".join(lines)


def cmd_today(wa_id, _args):
    t = db.today()
    rows = db.between(wa_id, t, t + timedelta(days=1))
    if not rows:
        return [("text", "Nothing logged today yet.")]
    total = sum(r["amount"] for r in rows)
    lines = [f"*Today* - {money(total)}"]
    lines += [f"• {money(r['amount'])} {r['description']} _({r['category']})_ #{r['id']}" for r in rows]
    return [("text", "\n".join(lines))]


def cmd_week(wa_id, _args):
    t = db.today()
    start = t - timedelta(days=t.weekday())
    text = period_summary(wa_id, start, t + timedelta(days=1), f"This week (since {start.strftime('%a %d %b')})")
    prev = sum(db.totals_by_category(wa_id, start - timedelta(days=7), t - timedelta(days=6)).values())
    if prev:
        text += f"\nSame point last week: {money(prev)}"
    return [("text", text)]


def cmd_month(wa_id, args):
    ym = _ym(args[0]) if args else None
    start, end = month_bounds(ym)
    label = start.strftime("%B %Y")
    totals = db.totals_by_category(wa_id, start, end)
    budgets = db.get_budgets(wa_id)
    text = period_summary(wa_id, start, end, label)
    out = [("text", text)]
    if totals:
        out.append(("image", charts.category_breakdown(totals, budgets, label), f"{label} by category"))
    return out


def cmd_cat(wa_id, args):
    if not args:
        return [("text", "Which category? e.g. /cat food")]
    ym = _ym(args[-1]) if len(args) > 1 else None
    name = " ".join(args[:-1] if ym else args)
    cat = categories.resolve(name, wa_id)
    if not cat:
        return [("text", f"Unknown category '{name}'. Options: " + ", ".join(categories.for_user(wa_id)))]
    start, end = month_bounds(ym)
    rows = [r for r in db.between(wa_id, start, end) if r["category"] == cat]
    if not rows:
        return [("text", f"No {cat} expenses in {start.strftime('%B %Y')}.")]
    total = sum(r["amount"] for r in rows)
    lines = [f"*{cat}* · {start.strftime('%B %Y')} - {money(total)}"]
    b = db.get_budgets(wa_id).get(cat)
    if b:
        lines.append(_pct_line("Budget", total, b))
    for r in rows[-25:]:
        lines.append(f"• {date.fromisoformat(r['spent_on']).strftime('%d %b')}  {money(r['amount'])}  {r['description']} #{r['id']}")
    if len(rows) > 25:
        lines.append(f"…and {len(rows) - 25} earlier")
    return [("text", "\n".join(lines))]


def cmd_trends(wa_id, _args, months=6):
    this_start, this_end = month_bounds()
    starts = [this_start]
    for _ in range(months - 1):
        prev = starts[0] - timedelta(days=1)
        starts.insert(0, date(prev.year, prev.month, 1))
    by_month = db.totals_by_month(wa_id, starts[0], this_end)
    series = [(s.strftime("%Y-%m"), by_month.get(s.strftime("%Y-%m"), 0.0)) for s in starts]
    if not any(v for _, v in series):
        return [("text", "Not enough data for trends yet - log a few expenses first.")]

    # Category movers: this month so far vs the same days last month
    t = db.today()
    last_start = starts[-2]
    same_day_last = min(t.day, monthrange(last_start.year, last_start.month)[1])
    now_c = db.totals_by_category(wa_id, this_start, t + timedelta(days=1))
    prev_c = db.totals_by_category(wa_id, last_start, last_start + timedelta(days=same_day_last))
    lines = [f"*This month vs same point last month* (day {t.day})"]
    movers = []
    for cat in set(now_c) | set(prev_c):
        a, b = now_c.get(cat, 0), prev_c.get(cat, 0)
        movers.append((a - b, cat, a, b))
    movers.sort(key=lambda m: abs(m[0]), reverse=True)
    for d, cat, a, b in movers[:5]:
        arrow = "🔺" if d > 0 else "🔻"
        lines.append(f"{arrow} {cat}: {money(a)} vs {money(b)} ({'+' if d > 0 else '-'}{money(abs(d))})")
    budgets = db.get_budgets(wa_id)
    return [
        ("image", charts.monthly_trend(series, budgets.get(db.TOTAL)), "Monthly spending"),
        ("text", "\n".join(lines)),
    ]


def cmd_daily(wa_id, _args):
    start, end = month_bounds()
    daily = db.totals_by_day(wa_id, start, end)
    if not daily:
        return [("text", "Nothing logged this month yet.")]
    budget = db.get_budgets(wa_id).get(db.TOTAL)
    by_cat = db.totals_by_category(wa_id, start, end)
    fixed = sum(by_cat.get(c, 0) for c in categories.FIXED)
    out = [("image", charts.daily_pace(daily, start, db.today(), budget, fixed), "This month's pace")]
    if not budget:
        out.append(("text", "Tip: set a monthly budget with /budget total 30000 to see if you're on pace."))
    return out


def cmd_recent(wa_id, args):
    n = int(args[0]) if args and args[0].isdigit() else 10
    rows = db.recent(wa_id, min(n, 50))
    if not rows:
        return [("text", "No expenses yet. Try: 220 swiggy")]
    lines = ["*Recent*"]
    for r in rows:
        d = date.fromisoformat(r["spent_on"]).strftime("%d %b")
        lines.append(f"#{r['id']} {d}  {money(r['amount'])}  {r['description']} _({r['category']})_")
    return [("text", "\n".join(lines))]


def cmd_budget(wa_id, args):
    if len(args) >= 2:
        name, amt = " ".join(args[:-1]), args[-1].replace(",", "").lower()
        try:
            value = float(amt[:-1]) * 1000 if amt.endswith("k") else float(amt)
        except ValueError:
            return [("text", "Usage: /budget food 6000  or  /budget total 30000")]
        cat = db.TOTAL if name.lower() in ("total", "overall", "monthly", "all") else categories.resolve(name, wa_id)
        if not cat:
            return [("text", f"Unknown category '{name}'. Options: " + ", ".join(categories.for_user(wa_id)))]
        db.set_budget(wa_id, cat, value)
        label = "Monthly total" if cat == db.TOTAL else cat
        return [("text", f"Removed budget for {label}." if value <= 0 else f"✅ {label} budget set to {money(value)}/month.")]

    budgets = db.get_budgets(wa_id)
    if not budgets:
        return [("text", "No budgets yet.\nSet one: /budget total 30000 or /budget food 6000")]
    start, end = month_bounds()
    totals = db.totals_by_category(wa_id, start, end)
    t = db.today()
    days_left = monthrange(t.year, t.month)[1] - t.day + 1
    lines = [f"*Budgets · {start.strftime('%B')}* ({days_left} days left)"]
    if db.TOTAL in budgets:
        spent = sum(totals.values())
        lines.append(_pct_line("Total", spent, budgets[db.TOTAL]))
        left = budgets[db.TOTAL] - spent
        if left > 0:
            lines.append(f"   {money(left)} left · ~{money(left / days_left)}/day")
    for cat, b in sorted(budgets.items()):
        if cat != db.TOTAL:
            lines.append(_pct_line(cat, totals.get(cat, 0), b))
    return [("text", "\n".join(lines))]


def cmd_undo(wa_id, _args):
    row = db.last_expense(wa_id)
    if not row:
        return [("text", "Nothing to undo.")]
    db.delete_expense(wa_id, row["id"])
    return [("text", f"🗑️ Deleted #{row['id']}: {money(row['amount'])} {row['description']}")]


def cmd_delete(wa_id, args):
    if not args or not args[0].lstrip("#").isdigit():
        return [("text", "Usage: /delete 12")]
    row = db.delete_expense(wa_id, int(args[0].lstrip("#")))
    if not row:
        return [("text", "No entry with that number.")]
    return [("text", f"🗑️ Deleted #{row['id']}: {money(row['amount'])} {row['description']}")]


def cmd_fix(wa_id, args):
    if len(args) < 2 or not args[0].lstrip("#").isdigit():
        return [("text", "Usage: /fix 12 groceries")]
    cat = categories.resolve(" ".join(args[1:]), wa_id)
    if not cat:
        return [("text", "Unknown category. Options: " + ", ".join(categories.for_user(wa_id)))]
    if not db.update_category(wa_id, int(args[0].lstrip("#")), cat):
        return [("text", "No entry with that number.")]
    return [("text", f"✏️ #{args[0].lstrip('#')} moved to {cat}.")]


def parse_amount(s: str) -> float | None:
    s = s.replace(",", "").replace("₹", "").lower().removeprefix("rs").strip()
    try:
        value = float(s[:-1]) * 1000 if s.endswith("k") else float(s)
    except ValueError:
        return None
    return value if value > 0 else None


def apply_edit(wa_id, exp_id, changes: dict):
    """Validates and applies changes to one expense. Keys: amount, category, description, date."""
    row = db.get_expense(wa_id, exp_id) if exp_id else db.last_expense(wa_id)
    if not row:
        return [("text", "No entry with that number." if exp_id else "Nothing to edit yet.")]
    fields = {}
    if changes.get("amount") is not None:
        try:
            amt = float(changes["amount"])
        except (TypeError, ValueError):
            amt = 0
        if amt <= 0:
            return [("text", "That amount doesn't look right. Try: /edit 12 4000")]
        fields["amount"] = amt
    if changes.get("category"):
        cat = categories.resolve(changes["category"], wa_id)
        if not cat:
            return [("text", "Unknown category. Options: " + ", ".join(categories.for_user(wa_id)))]
        fields["category"] = cat
    if changes.get("description"):
        fields["description"] = str(changes["description"])[:100]
    if changes.get("date"):
        try:
            d = date.fromisoformat(changes["date"])
        except (TypeError, ValueError):
            d = None
        if d and d <= db.today():
            fields["spent_on"] = d.isoformat()
    fields = {k: v for k, v in fields.items() if v != row[k]}
    if not fields:
        return [("text", f"#{row['id']} already says {money(row['amount'])} · {row['category']} · {row['description']}.")]
    db.update_expense(wa_id, row["id"], **fields)
    new = db.get_expense(wa_id, row["id"])
    was = []
    if "amount" in fields:
        was.append(money(row["amount"]))
    if "category" in fields:
        was.append(row["category"])
    if "description" in fields:
        was.append(row["description"])
    if "spent_on" in fields:
        was.append(date.fromisoformat(row["spent_on"]).strftime("%d %b"))
    d = date.fromisoformat(new["spent_on"])
    tag = "" if d == db.today() else f" · {d.strftime('%d %b')}"
    return [("text", f"✏️ #{new['id']} updated: {money(new['amount'])} · {new['category']} · "
                     f"{new['description']}{tag}\n_was {', '.join(was)}_")]


def cmd_edit(wa_id, args):
    if len(args) < 2 or not args[0].lstrip("#").isdigit():
        return [("text", "Usage: /edit 12 4000  (or /fix 12 groceries for the category)")]
    exp_id = int(args[0].lstrip("#"))
    amt = parse_amount(args[1]) if len(args) == 2 else None
    if amt is not None:
        return apply_edit(wa_id, exp_id, {"amount": amt})
    return apply_edit(wa_id, exp_id, {"category": " ".join(args[1:])})


def cmd_export(wa_id, args):
    ym = _ym(args[0]) if args else None
    start, end = month_bounds(ym)
    rows = db.between(wa_id, start, end)
    if not rows:
        return [("text", "Nothing to export for that month.")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "date", "amount", "category", "description", "merchant"])
    for r in rows:
        w.writerow([r["id"], r["spent_on"], r["amount"], r["category"], r["description"], r["merchant"]])
    name = f"expenses-{start.strftime('%Y-%m')}.csv"
    return [("document", buf.getvalue().encode(), name, "text/csv", f"{len(rows)} expenses")]


def cmd_report(wa_id, args):
    t = db.today()
    which = (args[0].lower() if args else "week").replace(" ", "")
    if which in ("week", "weekly", "w"):
        return reports.weekly(wa_id, reports.week_start(t), t + timedelta(days=1))
    if which in ("lastweek", "lw"):
        return reports.weekly(wa_id, reports.week_start(t) - timedelta(days=7))
    if which in ("month", "monthly", "m"):
        return reports.monthly(wa_id, t.replace(day=1), t + timedelta(days=1))
    if which in ("lastmonth", "lm"):
        return reports.monthly(wa_id, (t.replace(day=1) - timedelta(days=1)).replace(day=1))
    return [("text", "Usage: /report week · /report lastweek · /report month · /report lastmonth")]


def cmd_reports_toggle(wa_id, args):
    if not args or args[0].lower() not in ("on", "off"):
        u = db.get_user(wa_id)
        state = "on" if (u is None or u["reports_on"]) else "off"
        return [("text", f"Automatic reports are *{state}*. Use /reports on or /reports off.")]
    on = args[0].lower() == "on"
    db.set_reports_on(wa_id, on)
    if on:
        return [("text", "✅ Reports on: weekly on Sunday evening, monthly on the 1st.")]
    return [("text", "Reports off. You can still ask anytime with /report.")]


def cmd_categories(wa_id, _args):
    custom = db.custom_categories(wa_id)
    lines = ["*Your categories*", ", ".join(categories.CATEGORIES)]
    if custom:
        lines += ["", "*Your own:* " + ", ".join(custom)]
    lines += ["", "_Add one: add a category for Pets · Remove: remove the Pets category_"]
    return [("text", "\n".join(lines))]


def cmd_help(_wa_id, _args):
    return [("text", HELP)]


COMMANDS = {
    "help": cmd_help, "start": cmd_help, "today": cmd_today, "week": cmd_week,
    "month": cmd_month, "summary": cmd_month, "cat": cmd_cat, "category": cmd_cat,
    "trends": cmd_trends, "trend": cmd_trends, "daily": cmd_daily, "pace": cmd_daily,
    "recent": cmd_recent, "budget": cmd_budget, "budgets": cmd_budget, "undo": cmd_undo,
    "delete": cmd_delete, "fix": cmd_fix, "edit": cmd_edit, "export": cmd_export,
    "report": cmd_report, "reports": cmd_reports_toggle, "categories": cmd_categories,
}


def run(wa_id: str, text: str):
    """Returns replies if text is a command, else None."""
    parts = text.strip().split()
    if not parts:
        return None
    word = parts[0].lower()
    if word.startswith("/"):
        word = word[1:]
    elif len(parts) > 1:
        return None  # free text like "chai 20" is an expense; commands with args need a "/"
    fn = COMMANDS.get(word)
    if fn is None:
        return [("text", "Unknown command. Type /help.")] if parts[0].startswith("/") else None
    return fn(wa_id, parts[1:])
