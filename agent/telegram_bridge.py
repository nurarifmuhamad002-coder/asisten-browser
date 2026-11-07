import asyncio
import base64
import json
import os
from typing import TYPE_CHECKING, Any, Dict, Optional, Set

import httpx

if TYPE_CHECKING:
    from agent.orchestrator import AgentOrchestrator


class TelegramBotBridge:
    """
    Jembatan ke aplikasi Telegram asli di HP user.
    - Di server cloud (Hugging Face Spaces / SPACE_HOST), otomatis mendaftarkan HTTPS Webhook
      agar setiap chat dari HP langsung membangunkan & menggerakkan server 24 jam nonstop.
    - Di lokal / sandbox, otomatis memakai Long-Polling (getUpdates).
    """

    def __init__(self) -> None:
        self.token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.orchestrator: Optional["AgentOrchestrator"] = None
        space_host = os.getenv("SPACE_HOST", "").strip()
        default_pub = f"https://{space_host}" if space_host else ""
        self.public_web_url: str = os.getenv("PUBLIC_WEB_URL", default_pub).strip()
        self.use_webhook: bool = bool(space_host)

        self.active_chat_ids: Set[int] = set()
        self._poll_task: Optional[asyncio.Task] = None
        self._offset: int = 0
        self.bot_username: str = ""

    def attach_orchestrator(self, orchestrator: "AgentOrchestrator") -> None:
        self.orchestrator = orchestrator

    @property
    def is_configured(self) -> bool:
        return bool(self.token)

    def public_dict(self) -> Dict[str, Any]:
        return {
            "configured": self.is_configured,
            "bot_username": self.bot_username,
            "mode": "webhook" if self.use_webhook else "polling",
            "running": bool(self.use_webhook or (self._poll_task and not self._poll_task.done())),
            "masked_token": (self.token[:6] + "••••" + self.token[-4:]) if len(self.token) > 12 else "",
        }

    async def configure(self, token: str, public_web_url: str = "") -> Dict[str, Any]:
        await self.stop()
        clean = (token or "").strip()
        if public_web_url:
            self.public_web_url = public_web_url.strip()
        if not clean:
            self.token = ""
            self.bot_username = ""
            return self.public_dict()

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"https://api.telegram.org/bot{clean}/getMe")
            data = resp.json()
            if not data.get("ok"):
                raise ValueError("Token Bot Telegram tidak valid. Pastikan copy langsung dari @BotFather.")
            self.bot_username = data["result"].get("username", "")

            self.token = clean
            if self.use_webhook and self.public_web_url.startswith("https://"):
                wh_url = self.public_web_url.rstrip("/") + "/api/telegram/webhook"
                await client.post(
                    f"https://api.telegram.org/bot{clean}/setWebhook",
                    json={"url": wh_url},
                )
            else:
                await client.post(f"https://api.telegram.org/bot{clean}/deleteWebhook")
                self._poll_task = asyncio.create_task(self._poll_loop())

        return self.public_dict()

    async def start_if_env_set(self) -> None:
        if self.token:
            try:
                await self.configure(self.token, self.public_web_url)
            except Exception:
                pass

    async def stop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None

    async def process_webhook_update(self, upd: Dict[str, Any]) -> None:
        asyncio.create_task(self._handle_update(upd))

    async def _poll_loop(self) -> None:
        base_url = f"https://api.telegram.org/bot{self.token}"
        async with httpx.AsyncClient(timeout=35.0) as client:
            while self.token:
                try:
                    resp = await client.get(
                        f"{base_url}/getUpdates",
                        params={"offset": self._offset, "timeout": 25},
                    )
                    data = resp.json()
                    if not data.get("ok"):
                        await asyncio.sleep(3)
                        continue
                    for upd in data.get("result", []):
                        self._offset = max(self._offset, upd["update_id"] + 1)
                        await self._handle_update(upd)
                except asyncio.CancelledError:
                    break
                except Exception:
                    await asyncio.sleep(3)

    async def _handle_update(self, upd: Dict[str, Any]) -> None:
        if not self.orchestrator:
            return

        if "callback_query" in upd:
            cb = upd["callback_query"]
            cb_id = cb.get("id")
            data = cb.get("data", "")
            chat_id = cb.get("message", {}).get("chat", {}).get("id")
            if chat_id:
                self.active_chat_ids.add(chat_id)

            if data == "approve":
                await self.orchestrator.resolve_approval("approve")
                await self._answer_cb(cb_id, "Disetujui! Melanjutkan...")
            elif data == "reject":
                await self.orchestrator.resolve_approval("reject")
                await self._answer_cb(cb_id, "Dibatalkan.")
            elif data == "resume_login":
                await self.orchestrator.resume_from_takeover()
                await self._answer_cb(cb_id, "Melanjutkan pekerjaan...")
            return

        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return

        self.active_chat_ids.add(chat_id)

        if text.startswith("/start"):
            web_note = f"\n🌐 Ruang Web Live: {self.public_web_url}\n" if self.public_web_url else ""
            await self.send_text(
                chat_id,
                "Halo bro! 👋 Gua Asisten Browser lu yang siap jalan 24 jam.\n"
                f"{web_note}\n"
                "Tinggal ketik perintah biasa di sini, contoh:\n"
                "• Cari info kecerdasan buatan di Wikipedia lalu bikinin filenya\n"
                "• Buka portal akun gua, isi form pengajuan lalu proses bayar\n\n"
                "Perintah cepat:\n"
                "/layar — Kirim foto layar browser sekarang\n"
                "/lanjut — Lanjutkan kerja setelah lu selesai login\n"
                "/stop — Hentikan pekerjaan saat ini",
            )
            return

        if text.startswith("/stop"):
            await self.orchestrator.stop_goal()
            await self.send_text(chat_id, "⏹️ Pekerjaan dihentikan.")
            return

        if text.startswith("/lanjut"):
            await self.orchestrator.resume_from_takeover()
            await self.send_text(chat_id, "✅ Oke sip, gua lanjutin kerjanya!")
            return

        if text.startswith("/layar"):
            b64 = await self.orchestrator.browser.refresh_frame()
            if b64:
                cur_url = self.orchestrator.browser.page.url if self.orchestrator.browser.page else ""
                await self.send_photo_b64(chat_id, b64, f"🖥️ Layar saat ini: {cur_url}")
            else:
                await self.send_text(chat_id, "Browser sedang disiapkan...")
            return

        await self.send_text(chat_id, f"👍 Siap bro, lagi gua kerjain: {text}")
        await self.orchestrator.start_goal(text)

    async def _answer_cb(self, cb_id: Optional[str], text: str) -> None:
        if not cb_id or not self.token:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": text},
            )

    async def send_text(self, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
        if not self.token:
            return
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=payload)

    async def send_photo_b64(
        self,
        chat_id: int,
        photo_b64: str,
        caption: str = "",
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.token or not photo_b64:
            return
        raw_bytes = base64.b64decode(photo_b64)
        data: Dict[str, Any] = {"chat_id": str(chat_id), "caption": caption[:950]}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        files = {"photo": ("screen.jpg", raw_bytes, "image/jpeg")}
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.post(f"https://api.telegram.org/bot{self.token}/sendPhoto", data=data, files=files)

    async def send_document(self, chat_id: int, filename: str, file_bytes: bytes, caption: str = "") -> None:
        if not self.token:
            return
        data = {"chat_id": str(chat_id), "caption": caption[:950]}
        files = {"document": (filename, file_bytes)}
        async with httpx.AsyncClient(timeout=25.0) as client:
            await client.post(f"https://api.telegram.org/bot{self.token}/sendDocument", data=data, files=files)

    async def notify_takeover(self, reason: str, frame_b64: str) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        link_note = (
            f"\n\n🔑 Tap link ini buat login aman di layar:\n{self.public_web_url}"
            if self.public_web_url
            else "\n\n🔑 Buka layar web aplikasi buat ketik password dengan aman."
        )
        caption = f"🔐 Butuh Login Lu Bro\n{reason}{link_note}"
        markup = {
            "inline_keyboard": [[{"text": "✅ Udah Login — Lanjut Kerjain", "callback_data": "resume_login"}]]
        }
        for cid in list(self.active_chat_ids):
            if frame_b64:
                await self.send_photo_b64(cid, frame_b64, caption, markup)
            else:
                await self.send_text(cid, caption, markup)

    async def notify_approval(self, sens_report: Dict[str, Any], frame_b64: str) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        caption = (
            f"🛡️ Minta Izin Dulu Bro ({sens_report.get('category', '')})\n"
            f"Mau klik tombol: [{sens_report.get('target_label', '')}]\n"
            f"{sens_report.get('reason', '')}"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Setuju, Jalanin", "callback_data": "approve"},
                    {"text": "❌ Tolak / Batal", "callback_data": "reject"},
                ]
            ]
        }
        for cid in list(self.active_chat_ids):
            if frame_b64:
                await self.send_photo_b64(cid, frame_b64, caption, markup)
            else:
                await self.send_text(cid, caption, markup)

    async def notify_file(self, filename: str, file_bytes: bytes) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        for cid in list(self.active_chat_ids):
            await self.send_document(cid, filename, file_bytes, f"📄 File selesai dibuat: {filename}")

    async def notify_done(self, summary: str) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        clean = summary.replace("**", "")
        for cid in list(self.active_chat_ids):
            await self.send_text(cid, f"✅ Beres!\n\n{clean}")
