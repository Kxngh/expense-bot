"""End-to-end tests with WhatsApp and the LLM mocked out. Run: pytest -q"""
import asyncio
import hashlib
import hmac
import json
from datetime import timedelta

import pytest

from app import commands, config, db, handler, parser

USER = "919999999999"


class FakeWA:
    def __init__(self):
        self.sent = []

    async def send_text(self, to, body):
        self.sent.append(("text", body))

    async def send_image(self, to, png, caption=""):
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        self.sent.append(("image", caption))

    async def send_document(self, to, data, filename, mime, caption=""):
        self.sent.append(("document", filename))

    async def download_media(self, media_id):
        return b"fakejpeg", "image/jpeg"

    async def mark_read(self, msg_id):
        pass


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")  # use fallback parser by default
    monkeypatch.setattr(config, "LLM_PROVIDER", "none")
    monkeypatch.setattr(config, "PARSE_IMAGES", True)
    db.init()


def text(body, mid="m1"):
    return {"from": USER, "id": mid, "type": "text", "text": {"body": body}}


def run(msg):
    return asyncio.run(handler.handle(USER, msg, FakeWA()))


def all_text(replies):
    return "\n".join(r[1] for r in replies if r[0] == "text")


def test_fallback_parser_multiple_items():
    r = parser.fallback_parse("chai 20, auto 80\n1.2k shoes")
    assert [e.amount for e in r.expenses] == [20, 80, 1200]
    assert [e.category for e in r.expenses] == ["Food & Dining", "Transport", "Shopping"]


def test_log_and_summaries():
    out = all_text(run(text("220 swiggy")))
    assert "₹220" in out and "Food & Dining" in out
    run(text("uber 340"))
    assert "₹560" in all_text(run(text("/today")))
    replies = run(text("/month"))
    assert replies[0][0] == "text" and replies[1][0] == "image"


def test_budget_alerts():
    run(text("/budget food 1000"))
    run(text("/budget total 5000"))
    out = all_text(run(text("swiggy 850")))
    assert "⚠️ Food & Dining at 85%" in out
    out = all_text(run(text("zomato 200")))
    assert "🚨 Food & Dining budget exceeded" in out
    out = all_text(run(text("/budget")))
    assert "Total" in out and "Food & Dining" in out


def test_undo_delete_fix():
    run(text("milk 60"))
    run(text("pizza 400"))
    assert "Deleted" in all_text(run(text("/undo")))
    row = db.recent(USER)[0]
    assert "moved to Groceries" in all_text(run(text(f"/fix {row['id']} grocery")))
    assert db.recent(USER)[0]["category"] == "Groceries"
    assert "Deleted" in all_text(run(text(f"/delete {row['id']}")))
    assert db.recent(USER) == []


def test_edit_command():
    run(text("hinge membership 400"))
    row = db.recent(USER)[0]
    out = all_text(run(text(f"/edit {row['id']} 4000")))
    assert "₹4,000" in out and "was ₹400" in out
    assert db.get_expense(USER, row["id"])["amount"] == 4000
    assert "Groceries" in all_text(run(text(f"/edit #{row['id']} grocery")))
    assert "No entry" in all_text(run(text("/edit 999 50")))


def test_correction_in_plain_words_fallback():
    run(text("swiggy 220"))
    run(text("hinge membership 400"))
    out = all_text(run(text("oh no no its 4000 not 400")))
    assert "updated" in out and "₹4,000" in out
    rows = db.recent(USER)
    assert len(rows) == 2  # corrected, not logged as a new expense
    assert sorted(r["amount"] for r in rows) == [220, 4000]
    # an older entry with the matching wrong amount is picked over the latest one
    run(text("auto 80"))
    run(text("its 250 not 220"))
    assert sorted(r["amount"] for r in db.recent(USER)) == [80, 250, 4000]


def test_llm_edit_intent(monkeypatch):
    run(text("hinge membership 400"))
    row = db.recent(USER)[0]
    seen = {}

    async def fake_parse(text="", image=None, image_mime="", wa_id=None):
        seen["prompt"] = parser._system_prompt(wa_id)
        return parser._from_tool({"intent": "edit", "edit": {"id": row["id"], "amount": 4000, "category": "Bogus"}})
    monkeypatch.setattr(parser, "parse", fake_parse)
    out = all_text(run(text("oh no no its 4000 not 400")))
    assert f"#{row['id']}" in seen["prompt"]  # the model sees recent entries to know what to fix
    assert "updated" in out
    assert db.get_expense(USER, row["id"])["amount"] == 4000


def test_trends_and_daily_charts():
    t = db.today()
    for i in range(0, 150, 9):
        d = t - timedelta(days=i)
        db.add_expense(USER, 300 + i, "Food & Dining", "test", spent_on=d.isoformat())
        db.add_expense(USER, 120, "Transport", "test", spent_on=d.isoformat())
    db.set_budget(USER, db.TOTAL, 3000)
    kinds = [r[0] for r in run(text("/trends"))]
    assert kinds == ["image", "text"]
    assert run(text("/daily"))[0][0] == "image"


def test_export_csv():
    run(text("netflix 649"))
    r = run(text("/export"))
    assert r[0][0] == "document" and b"Netflix" in r[0][1]


def test_llm_command_routing(monkeypatch):
    async def fake_parse(text="", image=None, image_mime="", wa_id=None):
        return parser.ParseResult("command", command="/cat food")
    monkeypatch.setattr(parser, "parse", fake_parse)
    run(text("/budget food 2000"))
    db.add_expense(USER, 500, "Food & Dining", "Lunch")
    out = all_text(run(text("how much did i spend on food this month?")))
    assert "Food & Dining" in out and "₹500" in out


def test_receipt_image(monkeypatch):
    async def fake_parse(text="", image=None, image_mime="", wa_id=None):
        assert image == b"fakejpeg"
        return parser._from_tool({"intent": "log", "expenses": [
            {"amount": 1834.5, "category": "Groceries", "description": "DMart groceries",
             "merchant": "DMart", "date": db.today().isoformat()}]})
    monkeypatch.setattr(parser, "parse", fake_parse)
    msg = {"from": USER, "id": "img1", "type": "image", "image": {"id": "123", "mime_type": "image/jpeg"}}
    assert "₹1,834" in all_text(run(msg)) or "₹1.8k" in all_text(run(msg))


def test_webhook_signature_and_dedupe(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(config, "APP_SECRET", "s3cret")
    monkeypatch.setattr(config, "ALLOWED_NUMBERS", [USER])
    fake = FakeWA()
    db.update_user(USER, state="active")  # already onboarded
    with TestClient(main.app) as client:
        main.wa = fake
        assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": config.VERIFY_TOKEN,
                                              "hub.challenge": "42"}).text == "42"
        payload = {"entry": [{"changes": [{"value": {"messages": [text("chai 20", "wamid.1")]}}]}]}
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert client.post("/webhook", content=body, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 401
        for _ in range(2):  # second delivery is a retry and must be ignored
            assert client.post("/webhook", content=body, headers={"x-hub-signature-256": sig,
                                                                   "content-type": "application/json"}).status_code == 200
    assert len(fake.sent) == 1 and "₹20" in fake.sent[0][1]
    assert len(db.recent(USER)) == 1


def _gemini_mock(monkeypatch, handler_fn):
    import httpx
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(parser, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handler_fn)))


def test_gemini_text(monkeypatch):
    seen = {}

    def reply(request):
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers["x-goog-api-key"]
        out = {"intent": "log", "expenses": [
            {"amount": 450, "category": "Food & Dining", "description": "Dinner", "merchant": "",
             "date": db.today().isoformat()}]}
        return httpx_response(200, {"candidates": [{"content": {"parts": [
            {"text": "thinking...", "thought": True}, {"text": json.dumps(out)}]}}]})

    _gemini_mock(monkeypatch, reply)
    out = all_text(run(text("dinner 450 at that dhaba")))
    assert "₹450 · Food & Dining · Dinner" in out
    assert seen["key"] == "test-key"
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_rate_limited_falls_back(monkeypatch):
    _gemini_mock(monkeypatch, lambda req: httpx_response(429, {"error": {"code": 429}}))
    out = all_text(run(text("uber 340")))
    assert "₹340 · Transport" in out


def test_images_off_uses_caption_or_asks(monkeypatch):
    monkeypatch.setattr(config, "PARSE_IMAGES", False)
    called = []

    async def no_download(media_id):
        called.append(media_id)
        return b"", "image/jpeg"

    wa = FakeWA()
    wa.download_media = no_download
    msg = {"from": USER, "id": "i2", "type": "image", "image": {"id": "9", "mime_type": "image/jpeg"}}
    assert "Photos aren't turned on" in all_text(asyncio.run(handler.handle(USER, msg, wa)))
    msg["image"]["caption"] = "1834 dmart"
    assert "₹1,834" in all_text(asyncio.run(handler.handle(USER, msg, wa)))
    assert called == []  # the photo itself is never downloaded or sent anywhere


def httpx_response(status, body):
    import httpx
    return httpx.Response(status, json=body)
