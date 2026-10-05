"""Natural-language questions: the LLM's filter is mocked, the answer logic is real."""
import asyncio
from datetime import date, timedelta

import pytest

from app import config, db, handler, parser, query

USER, OTHER = "919999999999", "918888888888"


@pytest.fixture(autouse=True)
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "q.db"))
    monkeypatch.setattr(config, "LLM_PROVIDER", "none")
    db.init()
    # Fixed "today" so date maths is deterministic: Mon 20 Oct 2026
    monkeypatch.setattr(db, "today", lambda: date(2026, 10, 20))
    add = lambda amt, cat, desc, merch, d, who=USER: db.add_expense(who, amt, cat, desc, merch, d)
    add(300, "Food & Dining", "Lunch", "Swiggy", "2026-10-02")
    add(450, "Food & Dining", "Dinner", "Swiggy", "2026-10-15")
    add(220, "Food & Dining", "Biryani", "Zomato", "2026-10-18")
    add(80, "Transport", "Auto", "", "2026-10-14")
    add(90, "Transport", "Auto to office", "", "2026-10-16")
    add(60, "Transport", "Metro", "", "2026-10-19")
    add(2999, "Shopping", "Shoes", "Myntra", "2026-10-10")
    add(500, "Food & Dining", "Pizza", "Swiggy", "2026-09-12")
    add(700, "Food & Dining", "Team dinner", "", "2026-09-25")
    add(12000, "Rent", "Rent", "", "2026-09-01")
    add(99999, "Food & Dining", "Someone else's swiggy", "Swiggy", "2026-10-05", who=OTHER)


def ask(q: dict) -> str:
    return "\n".join(r[1] for r in query.answer(USER, q) if r[0] == "text")


def test_merchant_total_this_month():
    out = ask({"start": "2026-10-01", "end": "2026-10-20", "period_label": "this month",
               "search": "swiggy", "metric": "total", "group_by": "none"})
    assert "*₹750* on Swiggy this month (1–20 Oct)" in out
    assert "2 expenses" in out
    assert "99,999" not in out  # other users' data never leaks in


def test_count_with_custom_range():
    out = ask({"start": "2026-10-13", "end": "2026-10-19", "period_label": "last week",
               "search": "auto", "metric": "count", "group_by": "none"})
    assert out.startswith("*2 times*") and "₹170" in out


def test_largest():
    out = ask({"start": "2026-10-01", "end": "2026-10-20", "period_label": "this month",
               "metric": "largest", "group_by": "none", "limit": 2})
    lines = out.splitlines()
    assert "Biggest 2" in lines[0] and "₹2,999" in lines[1] and "₹450" in lines[2]


def test_average_per_day():
    out = ask({"start": "2026-10-14", "end": "2026-10-20", "period_label": "this past week",
               "categories": ["Transport"], "metric": "average_per_day", "group_by": "none"})
    assert "*₹33/day*" in out and "7 days" in out  # 230 / 7


def test_comparison():
    out = ask({"start": "2026-10-01", "end": "2026-10-31", "period_label": "this month",
               "categories": ["Food & Dining"], "metric": "total", "group_by": "none",
               "compare_start": "2026-09-01", "compare_end": "2026-09-30", "compare_label": "in September"})
    assert "this month: ₹970 (3×)" in out and "in September: ₹1,200 (2×)" in out
    assert "-₹230 (-19%)" in out


def test_breakdown_by_category_sends_chart():
    replies = query.answer(USER, {"start": "2026-10-01", "end": "2026-10-20", "period_label": "this month",
                                  "metric": "total", "group_by": "category"})
    assert replies[0][0] == "text" and "Shopping: ₹2,999" in replies[0][1]
    assert replies[1][0] == "image" and replies[1][1][:4] == b"\x89PNG"


def test_group_by_merchant():
    out = ask({"start": "2026-10-01", "end": "2026-10-20", "period_label": "this month",
               "categories": ["Food & Dining"], "metric": "total", "group_by": "merchant"})
    assert out.index("Swiggy: ₹750") < out.index("Zomato: ₹220")


def test_empty_and_bad_input_is_safe():
    assert "No expenses" in ask({"start": "2026-10-01", "end": "2026-10-20", "period_label": "this month",
                                 "search": "netflix", "metric": "total", "group_by": "none"})
    # garbage from the model: bad dates, unknown metric/category, future end date, injection-ish text
    out = ask({"start": "not-a-date", "end": "2099-01-01", "period_label": "",
               "categories": ["Hacking"], "search": "'; DROP TABLE expenses; --",
               "metric": "delete_everything", "group_by": "sql", "limit": "lots"})
    assert "No expenses" in out
    assert len(db.recent(USER, 50)) == 10  # nothing was touched


def test_end_to_end_through_handler(monkeypatch):
    async def fake_parse(text="", image=None, image_mime="", wa_id=None):
        return parser._from_tool({"intent": "query", "query": {
            "start": "2026-10-01", "end": "2026-10-20", "period_label": "this month",
            "search": "swiggy", "metric": "total", "group_by": "none"}})
    monkeypatch.setattr(parser, "parse", fake_parse)

    class WA:
        pass
    msg = {"from": USER, "id": "q1", "type": "text", "text": {"body": "how much did I blow on swiggy this month"}}
    out = asyncio.run(handler.handle(USER, msg, WA()))
    assert "₹750" in out[0][1]
