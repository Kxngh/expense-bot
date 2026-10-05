"""Answers natural-language questions.

The LLM only produces a filter (dates, categories, keyword, what to calculate).
This module validates it and runs it against the asking user's own rows - the LLM
never writes SQL and can never see or touch anyone else's data.
"""
from collections import defaultdict
from datetime import date, timedelta

from . import categories, charts, db
from .charts import money

METRICS = {"total", "count", "average_per_day", "average_per_expense", "largest", "list"}
GROUPS = {"none", "category", "merchant", "day", "week", "month"}
MAX_DAYS = 3 * 366  # cap absurd ranges


def _date(s, default: date) -> date:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return default


def _range(start_s, end_s) -> tuple[date, date]:
    t = db.today()
    end = min(_date(end_s, t), t)
    start = _date(start_s, end.replace(day=1))
    if start > end:
        start, end = end, start
    start = max(start, end - timedelta(days=MAX_DAYS))
    return start, end


def _span(start: date, end: date) -> str:
    if start == end:
        return start.strftime("%d %b")
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {end.strftime('%b')}"
    fmt = "%d %b" if start.year == end.year else "%d %b %Y"
    return f"{start.strftime(fmt)} – {end.strftime('%d %b %Y' if start.year != end.year else '%d %b')}"


def _rows(wa_id: str, start: date, end: date, cats: list[str], search: str):
    rows = db.between(wa_id, start, end + timedelta(days=1))
    if cats:
        rows = [r for r in rows if r["category"] in cats]
    if search:
        words = search.lower().split()
        rows = [r for r in rows
                if all(w in f"{r['merchant'] or ''} {r['description'] or ''}".lower() for w in words)]
    return rows


def _key(r, group: str) -> str:
    d = date.fromisoformat(r["spent_on"])
    if group == "category":
        return r["category"]
    if group == "merchant":
        return (r["merchant"] or r["description"] or "Unknown").strip().title()
    if group == "day":
        return d.strftime("%a %d %b")
    if group == "week":
        return "Week of " + (d - timedelta(days=d.weekday())).strftime("%d %b")
    return d.strftime("%b %Y")


def _sort_key(r, group: str):
    d = date.fromisoformat(r["spent_on"])
    if group == "day":
        return d
    if group == "week":
        return d - timedelta(days=d.weekday())
    if group == "month":
        return d.replace(day=1)
    return None


def answer(wa_id: str, q: dict) -> list:
    start, end = _range(q.get("start"), q.get("end"))
    metric = q.get("metric") if q.get("metric") in METRICS else "total"
    group = q.get("group_by") if q.get("group_by") in GROUPS else "none"
    allowed = categories.for_user(wa_id)
    asked = [str(c) for c in (q.get("categories") or [])]
    cats = [c for c in asked if c in allowed]
    if asked and not cats:  # don't silently widen to "everything"
        return [("text", f"No expenses for {', '.join(asked)}: you don't have that category. See /categories.")]
    search = (q.get("search") or "").strip()
    try:
        limit = max(1, min(int(q.get("limit") or 5), 25))
    except (TypeError, ValueError):
        limit = 5

    what = " / ".join(cats) if cats else ""
    if search:
        what = f"{search.title()}" + (f" ({what})" if what else "")
    what = what or "everything"
    label = (q.get("period_label") or "").strip() or "in that period"
    period = f"{label} ({_span(start, end)})"

    rows = _rows(wa_id, start, end, cats, search)

    # ---- comparison ----
    if q.get("compare_start") and q.get("compare_end"):
        c_start, c_end = _range(q["compare_start"], q["compare_end"])
        c_rows = _rows(wa_id, c_start, c_end, cats, search)
        a, b = sum(r["amount"] for r in rows), sum(r["amount"] for r in c_rows)
        c_label = (q.get("compare_label") or "").strip() or "the other period"
        lines = [f"*{what}*",
                 f"• {label}: {money(a)} ({len(rows)}×)  _{_span(start, end)}_",
                 f"• {c_label}: {money(b)} ({len(c_rows)}×)  _{_span(c_start, c_end)}_"]
        if b:
            d = a - b
            lines.append(f"{'🔺' if d > 0 else '🔻'} {'+' if d > 0 else '-'}{money(abs(d))} ({d / b:+.0%})")
        elif a:
            lines.append("Nothing in the other period to compare with.")
        if group == "category" and (rows or c_rows):
            ca, cb = defaultdict(float), defaultdict(float)
            for r in rows:
                ca[r["category"]] += r["amount"]
            for r in c_rows:
                cb[r["category"]] += r["amount"]
            moves = sorted(set(ca) | set(cb), key=lambda k: abs(ca[k] - cb[k]), reverse=True)[:6]
            lines.append("")
            lines += [f"• {k}: {money(ca[k])} vs {money(cb[k])}" for k in moves]
        return [("text", "\n".join(lines))]

    if not rows:
        return [("text", f"No expenses for {what} {period}.")]

    total = sum(r["amount"] for r in rows)
    n = len(rows)
    days = (end - start).days + 1

    # ---- breakdowns ----
    if group != "none":
        buckets, order = defaultdict(lambda: [0.0, 0]), {}
        for r in rows:
            k = _key(r, group)
            buckets[k][0] += r["amount"]
            buckets[k][1] += 1
            order.setdefault(k, _sort_key(r, group))
        if group in ("day", "week", "month"):
            keys = sorted(buckets, key=lambda k: order[k])
        else:
            keys = sorted(buckets, key=lambda k: buckets[k][0], reverse=True)
        lines = [f"*{what}* {period}: {money(total)} across {n} expenses"]
        shown = keys[-25:] if group in ("day", "week", "month") else keys[:15]
        for k in shown:
            s, c = buckets[k]
            val = f"{c}×" if metric == "count" else money(s)
            extra = f" · {c}×" if metric != "count" else f" · {money(s)}"
            lines.append(f"• {k}: {val}{extra}" + (f" ({s / total:.0%})" if group in ("category", "merchant") else ""))
        if len(keys) > len(shown):
            lines.append(f"…and {len(keys) - len(shown)} more")
        out = [("text", "\n".join(lines))]
        if group == "category" and len(keys) >= 3:
            out.append(("image", charts.category_breakdown({k: buckets[k][0] for k in keys}, {}, f"{label}"),
                        f"{what} {period}"))
        return out

    # ---- single answers ----
    if metric == "count":
        text = f"*{n} time{'s' if n != 1 else ''}* - {what} {period}\nTotal {money(total)} · avg {money(total / n)} each"
    elif metric == "average_per_day":
        text = f"*{money(total / days)}/day* on {what} {period}\n{money(total)} over {days} day{'s' if days != 1 else ''} · {n} expenses"
    elif metric == "average_per_expense":
        text = f"*{money(total / n)}* per expense on {what} {period}\n{n} expenses · {money(total)} total"
    elif metric in ("largest", "list"):
        picked = sorted(rows, key=lambda r: r["amount"], reverse=True)[:limit] if metric == "largest" \
            else sorted(rows, key=lambda r: (r["spent_on"], r["id"]), reverse=True)[:limit]
        head = "Biggest" if metric == "largest" else "Latest"
        lines = [f"*{head} {len(picked)}* - {what} {period}"]
        for r in picked:
            d = date.fromisoformat(r["spent_on"]).strftime("%d %b")
            lines.append(f"• {money(r['amount'])}  {r['description']} _({r['category']}, {d})_ #{r['id']}")
        lines.append(f"Total {money(total)} across {n} expenses")
        text = "\n".join(lines)
    else:
        text = f"*{money(total)}* on {what} {period}\n{n} expense{'s' if n != 1 else ''}"
        if n > 1:
            text += f" · avg {money(total / n)} each"
    return [("text", text)]
