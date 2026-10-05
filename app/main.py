"""FastAPI app exposing the WhatsApp webhook.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from . import config, db, handler, reports
from .whatsapp import WhatsApp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("expense-bot")

wa: WhatsApp | None = None


@asynccontextmanager
async def lifespan(_app):
    global wa
    db.init()
    if not config.APP_SECRET and not config.ALLOW_UNSIGNED_WEBHOOKS:
        log.error("APP_SECRET is not set: all webhooks will be rejected. Set it from App settings > Basic.")
    client = wa = WhatsApp()
    scheduler = asyncio.create_task(report_loop())
    yield
    scheduler.cancel()
    await client.http.aclose()
    db.close()


async def report_loop():
    """Checks once a minute whether a weekly/monthly report is due."""
    while True:
        try:
            n = await reports.tick(wa, handler.deliver)
            if n:
                log.info("Processed %d scheduled reports", n)
        except Exception:
            log.exception("Report scheduler error")
        await asyncio.sleep(60 - db.now().second)


app = FastAPI(title="WhatsApp Expense Bot", lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/tasks/reports")
async def run_reports(key: str = Query("")):
    """Wakes the app (if asleep) and sends any due weekly/monthly reports right away.

    Safe to call as often as you like: each report is only ever sent once.
    """
    if not config.CRON_SECRET or not hmac.compare_digest(key, config.CRON_SECRET):
        raise HTTPException(403, "Forbidden")
    n = await reports.tick(wa, handler.deliver)
    return {"ok": True, "reports": n}


@app.get("/webhook")
def verify(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    """Meta calls this once when you register the webhook URL."""
    if mode == "subscribe" and token == config.VERIFY_TOKEN:
        return PlainTextResponse(challenge)
    raise HTTPException(403, "Verification failed")


def _valid_signature(body: bytes, header: str | None) -> bool:
    if not config.APP_SECRET:
        # Without the secret anyone who finds the URL could post messages "from" any number
        # and read or change that person's data. Only allowed if explicitly enabled for local testing.
        return config.ALLOW_UNSIGNED_WEBHOOKS
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(config.APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


async def process(msg: dict, profile_name: str | None = None):
    wa_id = msg["from"]
    try:
        await wa.mark_read(msg["id"])
        user = db.get_user(wa_id)
        if user and user["state"] == "active":
            await reports.deliver_pending(wa, wa_id, handler.deliver)  # window just opened
        replies = await handler.handle_inbound(wa_id, msg, wa, profile_name)
    except Exception:
        log.exception("Failed handling message %s", msg.get("id"))
        replies = [("text", "Something went wrong saving that. Please try again.")]
    await handler.deliver(wa, wa_id, replies)


@app.post("/webhook")
async def receive(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not _valid_signature(body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "Bad signature")

    payload = await request.json()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", []) or []}
            for msg in value.get("messages", []) or []:
                sender = msg.get("from", "")
                if config.ALLOWED_NUMBERS and sender not in config.ALLOWED_NUMBERS:
                    log.warning("Ignoring message from unlisted number %s", sender)
                    continue
                if not db.mark_processed(msg["id"]):
                    continue  # Meta retried a webhook we already handled
                db.touch_user(sender)  # opens the 24h window for scheduled reports
                background.add_task(process, msg, names.get(sender))
    # Always 200 quickly, or Meta keeps retrying
    return {"status": "ok"}
