"""Onboarding, natural-language budgets/categories, and data deletion."""
import asyncio

import pytest

from app import categories, config, db, handler, parser

NEW = "916666666666"


class WA:
    pass


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "o.db"))
    monkeypatch.setattr(config, "LLM_PROVIDER", "none")
    monkeypatch.setattr(config, "PARSE_IMAGES", False)
    monkeypatch.setattr(config, "BOT_OWNER_NAME", "Krishna")
    db.init()


def say(text, who=NEW, name="Priya Sharma"):
    msg = {"from": who, "id": text, "type": "text", "text": {"body": text}}
    out = asyncio.run(handler.handle_inbound(who, msg, WA(), name))
    return "\n".join(r[1] for r in out if r[0] == "text")


def llm(monkeypatch, data):
    async def fake(text="", image=None, image_mime="", wa_id=None):
        return parser._from_tool(data, categories.for_user(wa_id))
    monkeypatch.setattr(parser, "parse", fake)


# ---------------- onboarding ----------------
def test_welcome_then_yes(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    out = say("hi")
    assert "Hi Priya!" in out and "Reply *YES*" in out
    assert "Krishna's server" in out and "Gemini" in out and "Photos aren't sent" in out
    assert db.get_user(NEW)["state"] == "awaiting_consent"
    out = say("yes")
    assert "You're all set, Priya" in out
    assert db.get_user(NEW)["state"] == "active"


def test_first_expense_is_kept_and_logged_after_yes():
    out = say("220 swiggy")
    assert "Reply *YES*" in out
    assert db.recent(NEW) == []                    # nothing stored before consent
    out = say("ok")
    assert "You're all set" in out and "₹220 · Food & Dining" in out
    assert len(db.recent(NEW)) == 1


def test_nothing_processed_before_consent(monkeypatch):
    called = []

    async def spy(*a, **k):
        called.append(1)
    monkeypatch.setattr(parser, "parse", spy)
    say("hello")
    say("how much on food?")                       # not YES -> gentle reminder, no AI call
    assert called == [] and "Just reply *YES*" in say("what is this")


def test_saying_no_stores_nothing():
    say("hi")
    assert "nothing's been saved" in say("no")
    assert db.get_user(NEW) is None


# ---------------- natural-language settings ----------------
@pytest.fixture
def active():
    db.update_user(NEW, state="active")


def test_budgets_in_plain_words(monkeypatch, active):
    llm(monkeypatch, {"intent": "settings", "settings": {"budgets": [
        {"category": "TOTAL", "amount": 30000}, {"category": "Food & Dining", "amount": 6000}]}})
    out = say("I want to spend 30k a month, 6k of it on food")
    assert "Monthly total budget: ₹30,000/month" in out and "Food & Dining budget: ₹6,000/month" in out
    assert db.get_budgets(NEW) == {"TOTAL": 30000, "Food & Dining": 6000}

    llm(monkeypatch, {"intent": "settings", "settings": {"budgets": [{"category": "Food & Dining", "amount": 0}]}})
    assert "Removed the Food & Dining budget" in say("drop the food budget")
    assert db.get_budgets(NEW) == {"TOTAL": 30000}


def test_custom_category_lifecycle(monkeypatch, active):
    llm(monkeypatch, {"intent": "settings", "settings": {
        "add_categories": ["pets"], "budgets": [{"category": "Pets", "amount": 2000}]}})
    out = say("add a category for my dog's stuff, pets, with a 2000 budget")
    assert "Added category *Pets*" in out and "Pets budget: ₹2,000/month" in out
    assert "Pets" in categories.for_user(NEW)
    assert "Pets" in parser.build_tool(categories.for_user(NEW))["input_schema"]["properties"]["expenses"]["items"]["properties"]["category"]["enum"]

    llm(monkeypatch, {"intent": "log", "expenses": [
        {"amount": 850, "category": "Pets", "description": "Dog food", "date": db.today().isoformat()}]})
    assert "₹850 · Pets · Dog food" in say("dog food 850")

    llm(monkeypatch, {"intent": "settings", "settings": {"remove_categories": ["Pets", "Food & Dining"]}})
    out = say("remove pets and food categories")
    assert "Removed *Pets* (1 expenses moved to Other)" in out
    assert "built-in category and can't be removed" in out
    assert db.recent(NEW)[0]["category"] == "Other"
    assert "Pets" not in db.get_budgets(NEW)


def test_settings_rejects_garbage(monkeypatch, active):
    llm(monkeypatch, {"intent": "settings", "settings": {
        "budgets": [{"category": "Food & Dining", "amount": -5}, {"category": "Nope", "amount": 100},
                    {"category": "Food & Dining", "amount": "lots"}],
        "add_categories": ["", "TOTAL", "*_*"]}})
    assert "didn't catch" in say("set things")
    assert db.get_budgets(NEW) == {} and db.custom_categories(NEW) == []


def test_custom_category_limit(monkeypatch, active):
    for i in range(categories.MAX_CUSTOM):
        db.add_category(NEW, f"Cat{i}")
    llm(monkeypatch, {"intent": "settings", "settings": {"add_categories": ["One More"]}})
    assert "Can't add One More" in say("add one more category")


# ---------------- deletion ----------------
def test_delete_my_data_needs_confirmation(active):
    db.add_expense(NEW, 100, "Other", "x")
    db.set_budget(NEW, db.TOTAL, 5000)
    db.add_category(NEW, "Pets")
    assert "Reply *DELETE*" in say("/deletemydata")
    assert "Cancelled" in say("no")
    assert len(db.recent(NEW)) == 1
    say("/deletemydata")
    assert "Deleted everything: 1 expenses" in say("DELETE")
    assert db.recent(NEW) == [] and db.get_budgets(NEW) == {} and db.custom_categories(NEW) == []
    assert db.get_user(NEW) is None
    assert "Reply *YES*" in say("hi")  # starts fresh with onboarding


def test_existing_users_skip_onboarding_after_upgrade():
    db.add_expense("915555555555", 50, "Other", "old data")
    db.update_user("915555555555", state="new")
    db.init()  # simulates restarting the upgraded server
    assert db.get_user("915555555555")["state"] == "active"
