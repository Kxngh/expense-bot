"""Weekly and monthly reports: what was spent, on what, and how.

Also the scheduling and delivery rules. WhatsApp only allows free-form messages within
24h of the user's last message, so a due report is either sent right away (window open)
or held and delivered with their next message (free), plus an optional paid template nudge.
"""
import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from . import categories, charts, config, db
from .charts import money

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=23, minutes=30)  # a little inside Meta's 24h, to be safe
MAX_LATE = timedelta(days=3)              # after downtime, don't send reports older than this


# ======================= stats =======================
def _variable(rows):
    """Day-to-day spending: rent/SIP-type costs skew weekly comparisons and 'how' stats."""
    return [r for r in rows if r["category"] not in categories.FIXED]


def _stats(rows, start: date, end: date) -> dict:
    """end is exclusive."""
    total = sum(r["amount"] for r in rows)
    days = (end - start).days
    by_cat = defaultdict(float)
    by_merchant = defaultdict(lambda: [0.0, 0])
    by_day = defaultdict(float)
    for r in rows:
        by_cat[r["category"]] += r["amount"]
        key = (r["merchant"] or r["description"] or "Other").strip().title()
        by_merchant[key][0] += r["amount"]
        by_merchant[key][1] += 1
        by_day[r["spent_on"]] += r["amount"]
    weekend = sum(v for d, v in by_day.items() if date.fromisoformat(d).weekday() >= 5)
    n_weekend_days = sum(1 for i in range(days) if (start + timedelta(days=i)).weekday() >= 5)
    weekday_days = days - n_weekend_days
    return {
        "total": total,
        "n": len(rows),
        "days": days,
        "per_day": total / days if days else 0,
        "by_cat": dict(sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)),
        "by_merchant": sorted(by_merchant.items(), key=lambda kv: kv[1][0], reverse=True),
        "frequent": sorted(by_merchant.items(), key=lambda kv: kv[1][1], reverse=True),
        "biggest": max(rows, key=lambda r: r["amount"]) if rows else None,
        "no_spend_days": days - len(by_day),
        "weekend_per_day": weekend / n_weekend_days if n_weekend_days else 0,
        "weekday_per_day": (total - weekend) / weekday_days if weekday_days else 0,
        "busiest_day": max(by_day.items(), key=lambda kv: kv[1]) if by_day else None,
    }


def _change(now: float, before: float) -> str:
    if not before:
        return ""
    d = (now - before) / before
    if abs(d) < 0.03:
        return "about the same as"
    return f"{'🔺' if d > 0 else '🔻'} {abs(d):.0%} {'more' if d > 0 else 'less'} than"


def _cat_lines(s: dict, prev: dict, limit=8) -> list[str]:
    lines = []
    for cat, v in list(s["by_cat"].items())[:limit]:
        p = prev["by_cat"].get(cat, 0)
        delta = ""
        if p:
            d = v - p
            if abs(d) >= max(50, 0.1 * p):
                delta = f"  ({'+' if d > 0 else '-'}{money(abs(d))})"
        elif v and cat not in categories.FIXED:
            delta = "  (new)"
        lines.append(f"• {cat}: {money(v)} · {v / s['total']:.0%}{delta}")
    rest = list(s["by_cat"].items())[limit:]
    if rest:
        lines.append(f"• Everything else: {money(sum(v for _, v in rest))}")
    return lines


def _how_lines(s: dict) -> list[str]:
    lines = []
    freq = [(k, c) for k, (_, c) in s["frequent"] if c >= 2][:3]
    if freq:
        lines.append("Most often: " + ", ".join(f"{k} ×{c}" for k, c in freq))
    top = s["by_merchant"][:3]
    if top:
        lines.append("Most money: " + ", ".join(f"{k} {money(v)}" for k, (v, _) in top))
    b = s["biggest"]
    if b:
        lines.append(f"Biggest: {money(b['amount'])} {b['description']} "
                     f"({date.fromisoformat(b['spent_on']).strftime('%a %d %b')})")
    if s["weekend_per_day"] and s["weekday_per_day"]:
        ratio = s["weekend_per_day"] / s["weekday_per_day"]
        if ratio >= 1.3:
            lines.append(f"Weekends cost {ratio:.1f}× a weekday ({money(s['weekend_per_day'])}/day vs {money(s['weekday_per_day'])})")
        elif ratio <= 0.75:
            lines.append(f"Weekdays cost more ({money(s['weekday_per_day'])}/day vs {money(s['weekend_per_day'])} on weekends)")
    if s["no_spend_days"]:
        lines.append(f"No-spend days: {s['no_spend_days']} 🎯")
    return lines


def _biggest_mover(s: dict, prev: dict) -> str:
    cats = (set(s["by_cat"]) | set(prev["by_cat"])) - categories.FIXED
    if not cats or not prev["total"]:
        return ""
    cat = max(cats, key=lambda c: abs(s["by_cat"].get(c, 0) - prev["by_cat"].get(c, 0)))
    d = s["by_cat"].get(cat, 0) - prev["by_cat"].get(cat, 0)
    if abs(d) < 200:
        return ""
    return f"💡 Biggest change: {cat} {'up' if d > 0 else 'down'} {money(abs(d))}"


# ======================= reports =======================
def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def weekly(wa_id: str, start: date, end: date | None = None) -> list:
    """Week of Monday `start`. `end` (exclusive) defaults to the full week; pass today+1 for 'so far'."""
    end = end or start + timedelta(days=7)
    rows = db.between(wa_id, start, end)
    s = _stats(rows, start, end)
    p_start = start - timedelta(days=7)
    prev = _stats(db.between(wa_id, p_start, p_start + (end - start)), p_start, p_start + (end - start))

    span = f"{start.strftime('%d %b')} – {(end - timedelta(days=1)).strftime('%d %b')}"
    if not rows:
        return [("text", f"📅 *Week of {span}*\nNothing logged this week.")]

    p_rows = db.between(wa_id, p_start, p_start + (end - start))
    v_now, v_prev = _stats(_variable(rows), start, end), _stats(_variable(p_rows), p_start, p_start + (end - start))
    fixed = s["total"] - v_now["total"]
    lines = [f"📅 *Weekly report · {span}*", f"*{money(s['total'])}* across {s['n']} expenses"]
    if fixed:
        lines.append(f"Day-to-day: {money(v_now['total'])} · {money(v_now['per_day'])}/day "
                     f"(+{money(fixed)} {' / '.join(c for c in categories.FIXED if c in s['by_cat']).lower()})")
    else:
        lines[-1] += f" · {money(s['per_day'])}/day"
    c1 = _change(v_now["total"], v_prev["total"])
    if c1:
        lines.append(f"{c1} last week ({money(v_prev['total'])})")
    four_var = [sum(r["amount"] for r in _variable(db.between(wa_id, start - timedelta(days=7 * k),
                                                                  start - timedelta(days=7 * (k - 1)))))
                for k in range(1, 5)]
    four_var = [v for v in four_var if v]
    if len(four_var) >= 2:
        avg4 = sum(four_var) / len(four_var)
        lines.append(f"{_change(v_now['total'], avg4)} your recent average ({money(avg4)}/week)")
    lines += ["", "*Where it went*", *_cat_lines(s, prev), "", "*How*", *_how_lines(v_now)]
    mover = _biggest_mover(s, prev)
    if mover:
        lines += ["", mover]
    lines += _budget_lines(wa_id)
    out = [("text", "\n".join(lines))]
    if len(s["by_cat"]) >= 2:
        out.append(("image", charts.category_breakdown(s["by_cat"], {}, f"week of {start.strftime('%d %b')}"),
                    f"Week of {span}"))
    return out


def monthly(wa_id: str, start: date, end: date | None = None) -> list:
    """Month starting `start`. `end` (exclusive) defaults to the full month."""
    full_end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    end = end or full_end
    rows = db.between(wa_id, start, end)
    s = _stats(rows, start, end)
    p_start = (start - timedelta(days=1)).replace(day=1)
    prev = _stats(db.between(wa_id, p_start, start), p_start, start)

    # last 6 months for the trend chart and average
    starts = [start]
    for _ in range(5):
        starts.insert(0, (starts[0] - timedelta(days=1)).replace(day=1))
    by_month = db.totals_by_month(wa_id, starts[0], full_end)
    series = [(m.strftime("%Y-%m"), by_month.get(m.strftime("%Y-%m"), 0.0)) for m in starts]
    past = [v for _, v in series[:-1] if v]
    avg = sum(past) / len(past) if past else 0

    label = start.strftime("%B %Y")
    if not rows:
        return [("text", f"🗓️ *{label}*\nNothing logged this month.")]

    lines = [f"🗓️ *Monthly report · {label}*", f"*{money(s['total'])}* across {s['n']} expenses · {money(s['per_day'])}/day"]
    if end == full_end:
        c1 = _change(s["total"], prev["total"])
        if c1:
            lines.append(f"{c1} {p_start.strftime('%B')} ({money(prev['total'])})")
        if len(past) >= 2:
            lines.append(f"{_change(s['total'], avg)} your {len(past)}-month average ({money(avg)})")
    lines += ["", "*Where it went*", *_cat_lines(s, prev, limit=10), "",
              "*How*", *_how_lines(_stats(_variable(rows), start, end))]

    budgets = db.get_budgets(wa_id)
    if budgets and end == full_end:
        lines += ["", "*Budgets*"]
        if db.TOTAL in budgets:
            b = budgets[db.TOTAL]
            ok = s["total"] <= b
            lines.append(f"{'✅' if ok else '🚨'} Total {money(s['total'])} of {money(b)}"
                         + (f" - saved {money(b - s['total'])}" if ok else f" - over by {money(s['total'] - b)}"))
        for cat, b in sorted(budgets.items()):
            if cat == db.TOTAL:
                continue
            v = s["by_cat"].get(cat, 0)
            lines.append(f"{'✅' if v <= b else '🚨'} {cat} {money(v)} of {money(b)}")
    mover = _biggest_mover(s, prev)
    if mover:
        lines += ["", mover]

    out = [("text", "\n".join(lines))]
    out.append(("image", charts.category_breakdown(s["by_cat"], budgets if end == full_end else {}, label), label))
    if sum(1 for _, v in series if v) >= 2:
        out.append(("image", charts.monthly_trend(series, budgets.get(db.TOTAL)), "Last 6 months"))
    return out


def _budget_lines(wa_id) -> list[str]:
    budgets = db.get_budgets(wa_id)
    if db.TOTAL not in budgets:
        return []
    t = db.today()
    m_start = t.replace(day=1)
    m_end = date(t.year + (t.month == 12), t.month % 12 + 1, 1)
    totals = db.totals_by_category(wa_id, m_start, m_end)
    spent = sum(totals.values())
    b = budgets[db.TOTAL]
    days_left = (m_end - t).days
    fixed = sum(totals.get(c, 0) for c in categories.FIXED)
    projected = fixed + (spent - fixed) / t.day * (m_end - m_start).days
    lines = ["", f"*{t.strftime('%B')} so far:* {money(spent)} of {money(b)} ({spent / b:.0%})"]
    if spent < b:
        lines.append(f"{money(b - spent)} left · ~{money((b - spent) / max(days_left, 1))}/day for {days_left} days")
    if projected > b * 1.05:
        lines.append(f"⚠️ At this pace you'll end near {money(projected)}")
    return lines


# ======================= scheduling =======================
def due_reports(now: datetime) -> list[tuple[str, str, date]]:
    """Which reports are due at `now`: [(kind, period_key, period_start)]."""
    due = []
    # Weekly: the most recent report time at or before now
    days_back = (now.weekday() - config.WEEKLY_REPORT_DAY) % 7
    at_day = now.date() - timedelta(days=days_back)
    at = datetime.combine(at_day, datetime.min.time(), now.tzinfo).replace(hour=config.WEEKLY_REPORT_HOUR)
    if at > now:
        at -= timedelta(days=7)
    if now - at <= MAX_LATE:
        # report on the Mon-Sun week that ends on/after the report day's week start
        ws = week_start(at.date()) if config.WEEKLY_REPORT_DAY >= 5 else week_start(at.date()) - timedelta(days=7)
        due.append(("weekly", ws.isoformat(), ws))
    # Monthly: on the 1st, about the previous month
    first = now.date().replace(day=1)
    at = datetime.combine(first, datetime.min.time(), now.tzinfo).replace(hour=config.MONTHLY_REPORT_HOUR)
    if at <= now and now - at <= MAX_LATE:
        ms = (first - timedelta(days=1)).replace(day=1)
        due.append(("monthly", ms.strftime("%Y-%m"), ms))
    return due


def build(wa_id: str, kind: str, start: date) -> list:
    if kind == "weekly":
        # a Sunday-evening report covers Mon..Sun; cap at today in case it's sent early
        end = min(start + timedelta(days=7), db.today() + timedelta(days=1))
        return weekly(wa_id, start, end)
    return monthly(wa_id, start)


def in_window(user) -> bool:
    if not user or not user["last_inbound"]:
        return False
    return db.now() - datetime.fromisoformat(user["last_inbound"]) < WINDOW


_handled: set[tuple[str, str]] = set()  # (kind, period) already processed by this process


async def tick(wa, deliver) -> int:
    """Run once a minute. Returns how many reports were sent or queued.

    Only touches the database when a report period is newly due, so an idle bot lets a
    serverless database (Neon) scale to zero instead of being woken every minute.
    """
    now = db.now()
    count = 0
    for kind, period, start in due_reports(now):
        if (kind, period) in _handled:
            continue
        _handled.add((kind, period))
        for user in db.all_users():
            wa_id = user["wa_id"]
            if user["state"] != "active" or not user["reports_on"] or not db.claim_report(wa_id, kind, period):
                continue
            end = start + timedelta(days=7) if kind == "weekly" else \
                date(start.year + (start.month == 12), start.month % 12 + 1, 1)
            if not db.between(wa_id, start, end):
                db.set_report_status(wa_id, kind, period, "skipped")
                continue
            count += 1
            if in_window(user):
                await deliver(wa, wa_id, build(wa_id, kind, start))
                db.set_report_status(wa_id, kind, period, "sent")
            else:
                db.set_report_status(wa_id, kind, period, "pending")
                if config.REPORT_TEMPLATE:
                    await wa.send_template(wa_id, config.REPORT_TEMPLATE, config.REPORT_TEMPLATE_LANG)
    return count


async def deliver_pending(wa, wa_id: str, deliver) -> int:
    """Called when a user messages us: their window is open, so send anything held."""
    sent = 0
    pending = db.pending_reports(wa_id)
    latest = {}
    for r in pending:  # if several piled up, only the newest of each kind is worth sending
        latest[r["kind"]] = r
    for r in pending:
        if latest[r["kind"]] is not r:
            db.set_report_status(wa_id, r["kind"], r["period"], "skipped")
    for r in latest.values():
        if r["kind"] == "weekly":
            start = date.fromisoformat(r["period"])
        else:
            y, m = map(int, r["period"].split("-"))
            start = date(y, m, 1)
        replies = build(wa_id, r["kind"], start)
        await deliver(wa, wa_id, [("text", "📬 Your report from while you were away:")] + replies)
        db.set_report_status(wa_id, r["kind"], r["period"], "delivered")
        sent += 1
    return sent
