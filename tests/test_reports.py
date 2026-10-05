"""Scheduled weekly/monthly reports and the WhatsApp 24h window."""
import asyncio
from datetime import date, datetime, timedelta

import pytest

from app import commands, config, db, handler, reports

USER, AWAY = "919999999999", "917777777777"
IST = config.TZ


class FakeWA:
    def __init__(self):
        self.sent = []

    async def send_text(self, to, body):
        self.sent.append((to, "text", body))

    async def send_image(self, to, png, caption=""):
        self.sent.append((to, "image", caption))

    async def send_document(self, *a, **k):
        pass

    async def send_template(self, to, name, lang="en"):
        self.sent.append((to, "template", name))


@pytest.fixture
def clock(monkeypatch):
    """Controllable clock. Starts Sun 11 Oct 2026, 8:00 PM IST."""
    state = {"now": datetime(2026, 10, 11, 20, 0, tzinfo=IST)}
    monkeypatch.setattr(db, "now", lambda: state["now"])
    monkeypatch.setattr(db, "today", lambda: state["now"].date())
    return state


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(config, "REPORT_TEMPLATE", "")
    monkeypatch.setattr(config, "WEEKLY_REPORT_DAY", 6)
    monkeypatch.setattr(config, "WEEKLY_REPORT_HOUR", 20)
    monkeypatch.setattr(config, "MONTHLY_REPORT_HOUR", 9)
    db.init()


def seed(who=USER):
    db.update_user(who, state="active")
    add = lambda amt, cat, desc, merch, d: db.add_expense(who, amt, cat, desc, merch, d)
    # this week (Mon 5 - Sun 11 Oct): spending on 5 of 7 days
    add(80, "Transport", "Auto", "", "2026-10-05")
    add(90, "Transport", "Auto", "", "2026-10-06")
    add(85, "Transport", "Auto", "", "2026-10-08")
    add(450, "Food & Dining", "Dinner", "Swiggy", "2026-10-06")
    add(1200, "Entertainment", "Movie + dinner", "PVR", "2026-10-10")
    add(900, "Food & Dining", "Brunch", "", "2026-10-11")
    add(1500, "Groceries", "Weekly groceries", "Blinkit", "2026-10-10")
    # last week
    add(300, "Food & Dining", "Lunch", "Swiggy", "2026-09-28")
    add(400, "Transport", "Cab", "Uber", "2026-10-01")
    # September
    add(12000, "Rent", "Rent", "", "2026-09-01")
    add(2500, "Shopping", "Shoes", "Myntra", "2026-09-15")


def run(coro):
    return asyncio.run(coro)


def test_weekly_report_content(clock):
    seed()
    replies = reports.weekly(USER, date(2026, 10, 5))
    text = replies[0][1]
    assert "Weekly report · 05 Oct – 11 Oct" in text
    assert "*₹4,305* across 7 expenses" in text
    assert "more than last week (₹700)" in text
    assert "Groceries: ₹1,500" in text and "(new)" in text
    assert "Most often: Auto ×3" in text
    assert "Biggest: ₹1,500 Weekly groceries" in text
    assert "Weekends cost" in text
    assert "No-spend days: 2" in text
    assert replies[1][0] == "image"


def test_monthly_report_with_budgets(clock):
    clock["now"] = datetime(2026, 10, 1, 9, 5, tzinfo=IST)
    seed()
    db.set_budget(USER, db.TOTAL, 20000)
    db.set_budget(USER, "Shopping", 2000)
    replies = reports.monthly(USER, date(2026, 9, 1))
    text = replies[0][1]
    assert "Monthly report · September 2026" in text
    assert "✅ Total ₹14,800 of ₹20,000 - saved ₹5,200" in text
    assert "🚨 Shopping ₹2,500 of ₹2,000" in text
    assert [r[0] for r in replies] == ["text", "image"]  # no trend chart with one month of data


def test_due_logic(clock):
    # Sunday 8:00 PM -> weekly for the week starting Mon 6 Oct
    assert reports.due_reports(clock["now"]) == [("weekly", "2026-10-05", date(2026, 10, 5))]
    # Sunday 7:59 PM -> not yet (and last week's is too old to resend)
    assert reports.due_reports(datetime(2026, 10, 11, 19, 59, tzinfo=IST)) == []
    # server was down Sunday night, back Monday 10 AM -> still catches up
    assert reports.due_reports(datetime(2026, 10, 12, 10, 0, tzinfo=IST))[0][1] == "2026-10-05"
    # 1st of the month, 9 AM -> monthly for previous month (plus weekly catch-up window)
    now = datetime(2026, 11, 1, 9, 0, tzinfo=IST)
    kinds = {k: p for k, p, _ in reports.due_reports(now)}
    assert kinds["monthly"] == "2026-10"
    # a week after the report time, nothing is due any more (no stale reports)
    assert reports.due_reports(datetime(2026, 10, 18, 10, 0, tzinfo=IST)) == []


def test_window_open_sends_now_and_never_twice(clock):
    seed()
    clock["now"] -= timedelta(hours=3)
    db.touch_user(USER)               # messaged at 5 PM
    clock["now"] += timedelta(hours=3)
    wa = FakeWA()
    assert run(reports.tick(wa, handler.deliver)) == 1
    assert any(kind == "text" and "Weekly report" in body for _, kind, body in wa.sent)
    assert db.report_status(USER, "weekly", "2026-10-05") == "sent"
    wa.sent.clear()
    assert run(reports.tick(wa, handler.deliver)) == 0  # next minute: already claimed
    assert wa.sent == []


def test_window_closed_holds_report_until_next_message(clock):
    seed(AWAY)
    clock["now"] -= timedelta(days=2)
    db.touch_user(AWAY)               # last message 2 days ago -> window closed
    clock["now"] += timedelta(days=2)
    wa = FakeWA()
    run(reports.tick(wa, handler.deliver))
    assert wa.sent == []              # nothing sent outside the window, no cost
    assert db.report_status(AWAY, "weekly", "2026-10-05") == "pending"

    # Monday morning they log something -> held report comes first
    clock["now"] += timedelta(hours=13)
    db.touch_user(AWAY)
    run(reports.deliver_pending(wa, AWAY, handler.deliver))
    assert "while you were away" in wa.sent[0][2]
    assert "Weekly report · 05 Oct – 11 Oct" in wa.sent[1][2]
    assert db.report_status(AWAY, "weekly", "2026-10-05") == "delivered"


def test_template_nudge_when_configured(clock, monkeypatch):
    monkeypatch.setattr(config, "REPORT_TEMPLATE", "report_ready")
    seed(AWAY)
    clock["now"] -= timedelta(days=2)
    db.touch_user(AWAY)
    clock["now"] += timedelta(days=2)
    wa = FakeWA()
    run(reports.tick(wa, handler.deliver))
    assert wa.sent == [(AWAY, "template", "report_ready")]


def test_opt_out_and_empty_weeks(clock):
    seed()
    db.touch_user(USER)
    db.touch_user("910000000000")  # a user with no expenses at all
    db.update_user("910000000000", state="active")
    commands.run(USER, "/reports off")
    wa = FakeWA()
    assert run(reports.tick(wa, handler.deliver)) == 0
    assert wa.sent == []
    assert db.report_status("910000000000", "weekly", "2026-10-05") == "skipped"
    assert "on" in commands.run(USER, "/reports on")[0][1]


def test_on_demand_commands(clock):
    seed()
    assert "Weekly report" in commands.run(USER, "/report")[0][1]
    assert "Weekly report · 28 Sep – 04 Oct" in commands.run(USER, "/report lastweek")[0][1]
    assert "October 2026" in commands.run(USER, "/report month")[0][1]
    assert "September 2026" in commands.run(USER, "/report lastmonth")[0][1]


def test_no_reports_before_onboarding(clock):
    seed(AWAY)
    db.update_user(AWAY, state="awaiting_consent")
    db.touch_user(AWAY)
    wa = FakeWA()
    assert run(reports.tick(wa, handler.deliver)) == 0 and wa.sent == []


def test_idle_scheduler_does_not_touch_database(clock, monkeypatch):
    """Neon's free tier has limited compute hours; a per-minute DB poll would use them all up."""
    seed()
    db.touch_user(USER)
    calls = []
    real = db.all_users
    monkeypatch.setattr(db, "all_users", lambda: calls.append(1) or real())
    wa = FakeWA()
    run(reports.tick(wa, handler.deliver))                  # report newly due -> one DB pass
    for minute in range(1, 120):                             # the next two hours of ticks
        clock["now"] += timedelta(minutes=1)
        run(reports.tick(wa, handler.deliver))
    assert len(calls) == 1
    clock["now"] = datetime(2026, 10, 14, 12, 0, tzinfo=IST)  # Wednesday: nothing due
    run(reports.tick(wa, handler.deliver))
    assert len(calls) == 1


def test_cron_endpoint_sends_due_reports(clock, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(config, "CRON_SECRET", "s3cret-cron")

    async def asleep():  # the in-process scheduler hasn't run: the cron call must do the work
        pass
    monkeypatch.setattr(main, "report_loop", asleep)
    seed()
    db.touch_user(USER)
    with TestClient(main.app) as client:
        wa = FakeWA()
        main.wa = wa
        assert client.get("/tasks/reports").status_code == 403
        assert client.get("/tasks/reports", params={"key": "wrong"}).status_code == 403
        r = client.get("/tasks/reports", params={"key": "s3cret-cron"})
        assert r.status_code == 200 and r.json()["reports"] == 1
        assert any("Weekly report" in m[2] for m in wa.sent if m[1] == "text")
        # the safety re-run 5 minutes later sends nothing new
        assert client.get("/tasks/reports", params={"key": "s3cret-cron"}).json()["reports"] == 0


def test_cron_endpoint_disabled_without_secret(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(config, "CRON_SECRET", "")
    with TestClient(main.app) as client:
        assert client.get("/tasks/reports", params={"key": ""}).status_code == 403
