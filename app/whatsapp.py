"""Thin client for the WhatsApp Cloud API."""
import logging

import httpx

from . import config

log = logging.getLogger(__name__)


class WhatsApp:
    def __init__(self, token: str = None, phone_number_id: str = None):
        self.token = token or config.WHATSAPP_TOKEN
        self.pnid = phone_number_id or config.PHONE_NUMBER_ID
        self.http = httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {self.token}"})

    async def _send(self, payload: dict):
        payload = {"messaging_product": "whatsapp", **payload}
        r = await self.http.post(f"{config.GRAPH_BASE}/{self.pnid}/messages", json=payload)
        if r.status_code >= 400:
            log.error("WhatsApp send failed %s: %s", r.status_code, r.text)
        return r

    async def send_text(self, to: str, body: str):
        # WhatsApp caps text bodies at 4096 chars
        return await self._send({"to": to, "type": "text", "text": {"body": body[:4096]}})

    async def upload_media(self, data: bytes, mime: str, filename: str) -> str | None:
        r = await self.http.post(
            f"{config.GRAPH_BASE}/{self.pnid}/media",
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (filename, data, mime)},
        )
        if r.status_code >= 400:
            log.error("Media upload failed %s: %s", r.status_code, r.text)
            return None
        return r.json().get("id")

    async def send_image(self, to: str, png: bytes, caption: str = ""):
        media_id = await self.upload_media(png, "image/png", "chart.png")
        if media_id:
            return await self._send({"to": to, "type": "image", "image": {"id": media_id, "caption": caption[:1024]}})

    async def send_document(self, to: str, data: bytes, filename: str, mime: str, caption: str = ""):
        media_id = await self.upload_media(data, mime, filename)
        if media_id:
            return await self._send({
                "to": to, "type": "document",
                "document": {"id": media_id, "filename": filename, "caption": caption[:1024]},
            })

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Inbound media arrives as an ID: first resolve its URL, then fetch the bytes with the token."""
        meta = (await self.http.get(f"{config.GRAPH_BASE}/{media_id}")).json()
        r = await self.http.get(meta["url"])
        r.raise_for_status()
        return r.content, meta.get("mime_type", "image/jpeg")

    async def send_template(self, to: str, name: str, lang: str = "en"):
        """Template messages are the only way to start a conversation outside the 24h window."""
        return await self._send({"to": to, "type": "template",
                                 "template": {"name": name, "language": {"code": lang}}})

    async def mark_read(self, msg_id: str):
        return await self._send({"status": "read", "message_id": msg_id})
