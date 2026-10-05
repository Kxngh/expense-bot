"""No data may cross between users.

Bob's data is full of unique markers. We run everything Alice can do - every command,
every kind of question, reports, exports, settings, and attempts to touch Bob's entries
by id - and check that no marker ever reaches Alice and Bob's data never changes.
"""
import asyncio
import json
from datetime import date, datetime, timedelta

import pytest

from app import categories, commands, config, db, handler, parser, query, reports

ALICE, BOB = "911111111111", "912222222222"
MARKERS = ["SECRETBOB", "77,777", "77777", "Bobcat", "Zorgshop", "66,666", "66666"]


@pytest.fixture(autouse=True)
def two_users(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "iso.db"))
    monkeypatch.setattr(config, "LLM_PROVIDER", "none")
    db.init()
    now = datetime(2026, 10, 11, 20, 0, tzinfo=config.TZ)
    monkeypatch.setattr(db, "now", lambda: now)
    monkeypatch.setattr(db, "today", lambda: now.date())
    for who in (ALICE, BOB):
        db.update_user(who, state="active")
        db.touch_user(who)

    # Alice: ordinary data
    for d, amt, cat, desc, merch in [
        ("2026-10-06", 220, "Food & Dining", "Lunch", "Swiggy"),
        ("2026-10-08", 80, "Transport", "Auto", ""),
        ("2026-10-10", 1500, "Groceries", "Groceries", "Blinkit"),
        ("2026-09-15", 999, "Shopping", "Shirt", "Myntra"),
    ]:
        db.add_expense(ALICE, amt, cat, desc, merch, d)
    db.set_budget(ALICE, db.TOTAL, 20000)

    # Bob: every field carries a marker, including a custom category and budgets
    db.add_category(BOB, "Bobcat")
    bob_ids = []
    for d, amt, cat in [("2026-10-06", 77777, "Food & Dining"), ("2026-10-09", 66666, "Bobcat"),
                        ("2026-09-20", 77777, "Shopping"), ("2026-10-10", 77777, "Transport")]:
        bob_ids.append(db.add_expense(BOB, amt, cat, "SECRETBOB swiggy auto", "Zorgshop", d))
    db.set_budget(BOB, db.TOTAL, 66666)
    db.set_budget(BOB, "Bobcat", 77777)
    return bob_ids


def bob_snapshot():
    with db.conn() as c:
        return {
            "expenses": [tuple(dict(r).values()) for r in c.execute("SELECT * FROM expenses WHERE wa_id=? ORDER BY id", (BOB,))],
            "budgets": sorted(tuple(dict(r).values()) for r in c.execute("SELECT * FROM budgets WHERE wa_id=?", (BOB,))),
            "cats": db.custom_categories(BOB),
        }


def leaked(replies) -> list[str]:
    blob = ""
    for r in replies or []:
        if r[0] == "text":
            blob += r[1]
        elif r[0] == "document":
            blob += r[1].decode()
    return [m for m in MARKERS if m in blob]


def as_alice(text):
    msg = {"from": ALICE, "id": text, "type": "text", "text": {"body": text}}
    return asyncio.run(handler.handle_inbound(ALICE, msg, None, "Alice"))


COMMANDS = [
    "/help", "/today", "/week", "/month", "/month 2026-09", "/cat food", "/cat transport 2026-10",
    "/cat bobcat", "/trends", "/daily", "/recent", "/recent 50", "/budget", "/export", "/export 2026-09",
    "/report week", "/report lastweek", "/report month", "/report lastmonth", "/categories",
    "/budget bobcat 5", "/fix 1 bobcat",
]


@pytest.mark.parametrize("cmd", COMMANDS)
def test_commands_never_show_other_users_data(cmd):
    before = bob_snapshot()
    assert leaked(as_alice(cmd)) == []
    assert bob_snapshot() == before


def test_cannot_touch_other_users_entries_by_id(two_users):
    before = bob_snapshot()
    for bob_id in two_users:
        for cmd in (f"/delete {bob_id}", f"/delete #{bob_id}", f"/fix {bob_id} groceries"):
            out = as_alice(cmd)
            assert "No entry with that number" in out[0][1] and leaked(out) == []
    as_alice("/undo")  # removes Alice's own last entry only
    assert bob_snapshot() == before


@pytest.mark.parametrize("q", [
    {"metric": "total", "group_by": "none"},
    {"metric": "total", "group_by": "category"},
    {"metric": "total", "group_by": "merchant"},
    {"metric": "count", "group_by": "day"},
    {"metric": "list", "group_by": "none", "limit": 25},
    {"metric": "largest", "group_by": "none", "limit": 25},
    {"metric": "average_per_day", "group_by": "week"},
    {"metric": "total", "group_by": "none", "search": "SECRETBOB"},
    {"metric": "total", "group_by": "none", "search": "zorgshop"},
    {"metric": "total", "group_by": "none", "categories": ["Bobcat"]},
    {"metric": "total", "group_by": "month", "start": "2020-01-01"},
    {"metric": "total", "group_by": "none", "compare_start": "2026-09-01", "compare_end": "2026-09-30"},
])
def test_questions_never_show_other_users_data(q):
    q = {"start": "2026-09-01", "end": "2026-10-11", "period_label": "lately", **q}
    out = query.answer(ALICE, q)
    if q.get("search") or q.get("categories"):
        # searching for Bob's words/category finds nothing (the reply only echoes Alice's own search term)
        assert out[0][1].startswith("No expenses")
        echoed = out[0][1].replace(q.get("search", "").title(), "")
        for c in q.get("categories", []):
            echoed = echoed.replace(c, "")
        out = [("text", echoed)]
    assert leaked(out) == []


def test_natural_language_settings_cant_reach_other_user(monkeypatch):
    before = bob_snapshot()

    async def fake(text="", image=None, image_mime="", wa_id=None):
        return parser._from_tool({"intent": "settings", "settings": {
            "remove_categories": ["Bobcat"], "budgets": [{"category": "Bobcat", "amount": 1}]}},
            categories.for_user(wa_id))
    monkeypatch.setattr(parser, "parse", fake)
    out = as_alice("delete the bobcat category and its budget")
    assert "don't have a category called Bobcat" in out[0][1]
    assert bob_snapshot() == before
    assert db.get_budgets(ALICE) == {db.TOTAL: 20000}


def test_ai_never_sees_other_users_categories_or_data(monkeypatch):
    """What would be sent to Gemini for Alice contains nothing of Bob's."""
    import httpx
    sent = []

    def capture(request):
        sent.append(request.content.decode())
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": json.dumps({"intent": "chat", "reply": "hi"})}]}}]})
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(parser, "_http", httpx.AsyncClient(transport=httpx.MockTransport(capture)))
    as_alice("how much did I spend lately?")
    assert sent and not any(m in sent[0] for m in MARKERS)
    assert "Bobcat" not in categories.for_user(ALICE)


def test_reports_are_per_user():
    seen = {}

    class WA:
        async def send_text(self, to, body):
            seen.setdefault(to, []).append(("text", body))

        async def send_image(self, to, png, caption=""):
            pass

        async def send_template(self, *a):
            pass
    asyncio.run(reports.tick(WA(), handler.deliver))
    assert leaked(seen[ALICE]) == []
    assert any("77,777" in b for _, b in seen[BOB])  # Bob does get his own report


def test_deleting_my_data_leaves_others_alone():
    before = bob_snapshot()
    as_alice("/deletemydata")
    as_alice("DELETE")
    assert db.recent(ALICE) == []
    assert bob_snapshot() == before


def test_unsigned_webhooks_rejected(monkeypatch):
    """Without APP_SECRET, a forged webhook could pose as any number - so it's refused."""
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(config, "APP_SECRET", "")
    monkeypatch.setattr(config, "ALLOW_UNSIGNED_WEBHOOKS", False)
    forged = {"entry": [{"changes": [{"value": {"messages": [
        {"from": BOB, "id": "forged", "type": "text", "text": {"body": "/deletemydata"}}]}}]}]}
    with TestClient(main.app) as client:
        assert client.post("/webhook", json=forged).status_code == 401
    assert db.get_user(BOB)["pending_action"] is None
