"""PNG charts sent back into WhatsApp. Rendered with matplotlib (no display needed)."""
import io
import re
from calendar import monthrange
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from . import config  # noqa: E402

# Palette (light surface)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"
BLUE_LIGHT = "#86b6ef"
WARNING = "#fab219"
CRITICAL = "#d03b3b"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 11,
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK_2,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.spines.top": False,
    "axes.spines.right": False,
})


def _indian(n: int) -> str:
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    return ("-" if n < 0 else "") + s


def money(v: float, short: bool = False) -> str:
    """Full amounts with Indian digit grouping (1,20,000); short=True for chart labels (1.2L, 12k)."""
    sym = config.CURRENCY
    if short and abs(v) >= 100000:
        return f"{sym}{v / 100000:.1f}L".replace(".0L", "L")
    if short and abs(v) >= 1000:
        return f"{sym}{v / 1000:.1f}k".replace(".0k", "k")
    return f"{sym}{_indian(round(v))}"


def money_s(v):
    return money(v, short=True)


def _fig(w=8, h=5):
    fig, ax = plt.subplots(figsize=(w, h), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    return fig, ax


def _title(fig, title, subtitle=""):
    fig.text(0.04, 0.95, title, fontsize=15, fontweight="bold", color=INK, va="top")
    if subtitle:
        fig.text(0.04, 0.895, subtitle, fontsize=10.5, color=INK_2, va="top")


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()


def category_breakdown(totals: dict[str, float], budgets: dict[str, float], month_label: str) -> bytes:
    """Horizontal bars per category, with budget ticks and over-budget highlighting."""
    items = sorted(totals.items(), key=lambda kv: kv[1])  # smallest at bottom -> largest on top
    n = max(len(items), 1)
    fig, ax = _fig(8, 1.6 + 0.48 * n)
    spent = sum(totals.values())
    total_budget = budgets.get("TOTAL")
    sub = f"{money_s(spent)} spent"
    if total_budget:
        sub += f" of {money_s(total_budget)} budget ({spent / total_budget:.0%})"
    _title(fig, f"Spending by category · {month_label}", sub)

    labels = [k for k, _ in items]
    values = [v for _, v in items]
    xmax = max([*values, *[budgets.get(k, 0) for k in labels], 1]) * 1.32

    for i, (cat, val) in enumerate(items):
        b = budgets.get(cat)
        color = BLUE
        if b and val > b:
            color = CRITICAL
        elif b and val >= 0.8 * b:
            color = WARNING
        ax.barh(i, val, height=0.55, color=color, edgecolor=SURFACE, linewidth=2)
        if b:
            ax.plot([b, b], [i - 0.38, i + 0.38], color=INK, linewidth=2, solid_capstyle="butt")
        label = money_s(val) + (f" / {money_s(b)}" if b else "")
        if b and val > b:
            label += "  over"
        ax.text(max(val, b or 0) + xmax * 0.015, i, label, va="center", fontsize=10, color=INK_2)

    ax.set_yticks(range(len(labels)), labels, color=INK, fontsize=10.5)
    ax.set_xlim(0, xmax)
    ax.xaxis.set_visible(False)
    ax.spines["bottom"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    if not items:
        ax.text(0.5, 0.5, "No expenses yet", ha="center", transform=ax.transAxes, color=MUTED)
    if budgets:
        fig.text(0.04, 0.02, "│ = category budget   orange = 80%+ used   red = over budget",
                 fontsize=8.5, color=MUTED)
    fig.subplots_adjust(left=0.27, right=0.97, top=1 - 1.0 / (1.6 + 0.48 * n), bottom=0.6 / (1.6 + 0.48 * n))
    return _png(fig)


def monthly_trend(month_totals: list[tuple[str, float]], total_budget: float | None) -> bytes:
    """Bars of total spend per month; the current month is highlighted."""
    fig, ax = _fig(8, 4.8)
    labels = [date.fromisoformat(m + "-01").strftime("%b %y") for m, _ in month_totals]
    values = [v for _, v in month_totals]
    past = [v for v in values[:-1] if v]
    sub = f"This month so far {money_s(values[-1])}" if values else ""
    if past:
        sub += f" · past average {money_s(sum(past) / len(past))}/month"
    _title(fig, f"Monthly spending · last {len(values)} months", sub)

    colors = [BLUE_LIGHT] * (len(values) - 1) + [BLUE]
    bars = ax.bar(range(len(values)), values, width=0.6, color=colors, edgecolor=SURFACE, linewidth=2)
    for b, v in zip(bars, values):
        if v:
            ax.text(b.get_x() + b.get_width() / 2, v, money_s(v), ha="center", va="bottom", fontsize=9.5, color=INK_2)
    if total_budget:
        ax.axhline(total_budget, color=INK_2, linestyle=(0, (4, 3)), linewidth=1.5)
        ax.text(len(values) - 0.5, total_budget, f" budget {money_s(total_budget)}",
                va="bottom", ha="right", fontsize=9, color=INK_2)
    ax.set_xticks(range(len(labels)), labels)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: money_s(v)))
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)
    ax.set_ylim(0, max([*values, total_budget or 0, 1]) * 1.15)
    fig.subplots_adjust(left=0.1, right=0.97, top=0.8, bottom=0.1)
    return _png(fig)


def daily_pace(daily: dict[str, float], month_start: date, today: date, total_budget: float | None,
               fixed: float = 0.0) -> bytes:
    """Cumulative spend this month vs. an even-pace line to the budget.

    `fixed` = one-off monthly costs already paid (rent, SIPs). They're excluded from the
    daily rate so paying rent on the 1st doesn't project a 5x month.
    """
    days_in_month = monthrange(month_start.year, month_start.month)[1]
    last_day = today.day if (today.year, today.month) == (month_start.year, month_start.month) else days_in_month
    xs, cum, running = [], [], 0.0
    for d in range(1, last_day + 1):
        running += daily.get(date(month_start.year, month_start.month, d).isoformat(), 0)
        xs.append(d)
        cum.append(running)

    fig, ax = _fig(8, 4.8)
    sub = f"{money_s(running)} by day {last_day}"
    if total_budget:
        diff = running - _pace(total_budget, fixed, last_day, days_in_month)
        sub += f" · {money_s(abs(diff))} {'over' if diff > 0 else 'under'} pace"
    projected = fixed + (running - fixed) / last_day * days_in_month
    sub += f" · heading for ~{money_s(projected)}"
    _title(fig, f"Spending pace · {month_start.strftime('%B %Y')}", sub)

    if total_budget:
        ax.plot([0, days_in_month], [min(fixed, total_budget), total_budget], color=MUTED,
                linestyle=(0, (4, 3)), linewidth=1.5)
        ax.text(days_in_month, total_budget, f"budget {money_s(total_budget)} ", ha="right", va="bottom",
                fontsize=9, color=INK_2)
    over = bool(total_budget) and running > _pace(total_budget, fixed, last_day, days_in_month)
    color = CRITICAL if over else BLUE
    ax.plot([0, *xs], [0, *cum], color=color, linewidth=2.2)
    ax.scatter([xs[-1]], [cum[-1]], s=40, color=color, edgecolors=SURFACE, linewidths=2, zorder=3)
    ax.text(xs[-1] + 0.6, cum[-1], money_s(cum[-1]), va="center", fontsize=10, color=INK, zorder=4,
            bbox=dict(boxstyle="round,pad=0.25", facecolor=SURFACE, edgecolor="none"))

    ax.set_xlim(0, days_in_month + 0.5)
    ax.set_ylim(0, max(running, total_budget or 0, 1) * 1.15)
    ax.set_xlabel("Day of month", fontsize=10)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: money_s(v)))
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)
    fig.subplots_adjust(left=0.1, right=0.95, top=0.8, bottom=0.13)
    return _png(fig)


def _pace(budget, fixed, day, days_in_month):
    """Where you'd be on `day` if fixed costs are paid up front and the rest is spread evenly."""
    fixed = min(fixed, budget)
    return fixed + (budget - fixed) * day / days_in_month
