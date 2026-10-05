"""Serverless mode (Vercel): no startup hook, work finished before the response returns."""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import config, db, main

USER = "919999999999"


class FakeWA:
    def __init__(self):
        self.sent = []

    async def send_text(self, to, body):
        self.sent.append((to, body))

    async def send_image(self, *a, **k):
        pass

    async def send_document(self, *a, **k):
        pass

    async def send_template(self, *a, **k):
        pass

    async def mark_read(self, msg_id):
        pass


@pytest.fixture
def serverless(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "sl.db"))  # brand-new, no tables yet
    monkeypatch.setattr(config, "SERVERLESS", True)
    monkeypatch.setattr(config, "APP_SECRET", "s3cret")
    monkeypatch.setattr(config, "CRON_SECRET", "cron")
    monkeypatch.setattr(config, "ALLOWED_NUMBERS", [USER])
    monkeypatch.setattr(config, "LLM_PROVIDER", "none")
    monkeypatch.setattr(main, "_db_ready", False)
    fake = FakeWA()
    monkeypatch.setattr(main, "wa", fake)
    return fake


def post(client, text, mid):
    body = json.dumps({"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": USER, "profile": {"name": "Riya"}}],
        "messages": [{"from": USER, "id": mid, "type": "text", "text": {"body": text}}]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body,
                       headers={"x-hub-signature-256": sig, "content-type": "application/json"})


def test_works_without_startup_hook(serverless):
    client = TestClient(main.app)  # not used as a context manager -> lifespan never runs
    assert post(client, "220 swiggy", "m1").status_code == 200
    # the reply was sent before the response returned, and tables were created on demand
    assert "Hi Riya!" in serverless.sent[0][1]
    post(client, "yes", "m2")
    assert any("₹220 · Food & Dining" in body for _, body in serverless.sent)
    assert len(db.recent(USER)) == 1
    post(client, "yes", "m2")  # Meta retry of the same message: ignored
    assert len(db.recent(USER)) == 1
    assert client.get("/tasks/reports", params={"key": "cron"}).status_code == 200
