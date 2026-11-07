import asyncio
import base64
import json
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

import httpx

if TYPE_CHECKING:
    from agent.orchestrator import AgentOrchestrator


MAIN_MENU_KB: Dict[str, Any] = {
    "keyboard": [
        [{"text": "💬 Ngobrol AI"}, {"text": "📝 Kerjain Tugas Web"}],
        [{"text": "🌐 Buka Web"}, {"text": "📸 Cek Layar"}],
        [{"text": "📂 Daftar File"}, {"text": "⏹️ Stop / Menu"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

BOT_COMMANDS: List[Dict[str, str]] = [
    {"command": "menu", "description": "Tampilkan Menu Utama"},
    {"command": "ngobrol", "description": "Mode Ngobrol AI Cepat (tanpa browser)"},
    {"command": "tugas", "description": "Mode Kerjain Tugas Otomatis di Web"},
    {"command": "buka", "description": "Buka Website & Login Aman"},
    {"command": "layar", "description": "Kirim Foto Layar Browser Sekarang"},
    {"command": "stop", "description": "Hentikan Proses Saat Ini"},
]


class TelegramBotBridge:
    """
    Jembatan interaktif ke Telegram di HP user dengan Menu Tombol Langsung:
    1. 💬 Ngobrol AI — Tanya jawab & bantuan tugas instan (<1 detik) tanpa buka browser
    2. 📝 Kerjain Tugas Web — AI mengoperasikan browser multi-langkah + buat file
    3. 🌐 Buka Web — Buka website apa saja + tombol layar live untuk login aman
    """

    def __init__(self) -> None:
        self.token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.orchestrator: Optional["AgentOrchestrator"] = None

        render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
        railway_dom = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
        space_host = os.getenv("SPACE_HOST", "").strip()

        auto_url = (
            render_url
            or (f"https://{railway_dom}" if railway_dom else "")
            or (f"https://{space_host}" if space_host else "")
        )
        self.public_web_url: str = (os.getenv("PUBLIC_WEB_URL", "").strip() or auto_url).rstrip("/")
        self.use_webhook: bool = bool(
            render_url or railway_dom or space_host or os.getenv("PUBLIC_WEB_URL", "").strip().startswith("https://")
        )

        self.active_chat_ids: Set[int] = set()
        self.chat_modes: Dict[int, str] = {}  # "chat" | "agent" | "open_web"
        self._status_msg_ids: Dict[int, int] = {}
        self._seen_update_ids: Set[int] = set()
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
            self.public_web_url = public_web_url.strip().rstrip("/")
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
            try:
                await client.post(
                    f"https://api.telegram.org/bot{clean}/setMyCommands",
                    json={"commands": BOT_COMMANDS},
                )
            except Exception:
                pass

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

    def _live_browser_inline_kb(self, include_task_btn: bool = True) -> Dict[str, Any]:
        rows: List[List[Dict[str, Any]]] = []
        if self.public_web_url.startswith("https://"):
            rows.append([{"text": "🖥️ Buka & Kontrol Layar Live", "url": self.public_web_url}])
        action_row: List[Dict[str, Any]] = [{"text": "🔄 Foto Ulang Layar", "callback_data": "snap_screen"}]
        if include_task_btn:
            action_row.insert(0, {"text": "📝 Kerjain di Web Ini", "callback_data": "mode_agent"})
        rows.append(action_row)
        return {"inline_keyboard": rows}

    async def _send_main_menu(self, chat_id: int, prefix: str = "") -> None:
        cur_mode = self.chat_modes.get(chat_id, "chat")
        mode_labels = {
            "chat": "💬 Ngobrol AI Cepat",
            "agent": "📝 Kerjain Tugas Web",
            "open_web": "🌐 Buka Web",
        }
        web_line = f"🌐 Layar Live: {self.public_web_url}\n" if self.public_web_url else ""
        body = (
            f"{prefix}"
            "🤖 Menu Utama Asisten Lu:\n"
            f"Mode aktif sekarang: {mode_labels.get(cur_mode, '💬 Ngobrol AI')}\n"
            f"{web_line}\n"
            "Tinggal tap tombol menu di bawah layar HP lu:\n"
            "• 💬 Ngobrol AI — Tanya jawab & bantu tugas kilat\n"
            "• 📝 Kerjain Tugas Web — Suruh AI jalanin web & bikin file\n"
            "• 🌐 Buka Web — Bukain web tujuan & login aman\n"
            "• 📸 Cek Layar — Intip foto layar browser sekarang\n"
            "• 📂 Daftar File — Ambil file tugas yang udah dibuat"
        )
        await self.send_text(chat_id, body, reply_markup=MAIN_MENU_KB)

    async def _handle_update(self, upd: Dict[str, Any]) -> None:
        if not self.orchestrator:
            return

        uid = upd.get("update_id")
        if uid is not None:
            if uid in self._seen_update_ids:
                return
            self._seen_update_ids.add(uid)
            if len(self._seen_update_ids) > 500:
                self._seen_update_ids.clear()

        # -------------------- INLINE BUTTON CALLBACKS --------------------
        if "callback_query" in upd:
            cb = upd["callback_query"]
            cb_id = cb.get("id")
            data = cb.get("data", "")
            chat_id = cb.get("message", {}).get("chat", {}).get("id")
            if not chat_id:
                return
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
            elif data == "snap_screen":
                await self._answer_cb(cb_id, "Mengambil foto layar...")
                await self._send_screen_snapshot(chat_id)
            elif data == "mode_agent":
                self.chat_modes[chat_id] = "agent"
                await self._answer_cb(cb_id, "Mode Kerjain Tugas Web aktif!")
                cur_u = self.orchestrator.browser.page.url if self.orchestrator.browser.page else ""
                await self.send_text(
                    chat_id,
                    f"📝 Sip bro! Sekarang tulis apa yang mau gua kerjain di web ini ({cur_u}):",
                    reply_markup=MAIN_MENU_KB,
                )
            elif data.startswith("dl_file:"):
                fname = data.split(":", 1)[1]
                await self._answer_cb(cb_id, f"Mengirim {fname}...")
                try:
                    fpath = self.orchestrator.file_manager.get_file_path(fname)
                    await self.send_document(chat_id, fpath.name, fpath.read_bytes(), f"📄 File: {fpath.name}")
                except Exception as exc:
                    await self.send_text(chat_id, f"Gagal mengambil file: {exc}")
            return

        # -------------------- TEXT / MENU MESSAGES --------------------
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return

        self.active_chat_ids.add(chat_id)
        low = text.lower().strip()

        # 1. START / MENU / STOP
        if low in ("/start", "/menu", "menu"):
            if chat_id not in self.chat_modes:
                self.chat_modes[chat_id] = "chat"
            await self._send_main_menu(chat_id, "Halo bro! 👋 ")
            return

        if text == "⏹️ Stop / Menu" or low in ("/stop", "stop", "berhenti", "batal"):
            await self.orchestrator.stop_goal(silent=True)
            await self._send_main_menu(chat_id, "⏹️ Proses yang lagi jalan udah dihentikan.\n\n")
            return

        # 2. LANJUT SETELAH LOGIN
        if low in ("/lanjut", "lanjut", "udah login", "sudah login"):
            await self.orchestrator.resume_from_takeover()
            await self.send_text(chat_id, "✅ Oke sip, gua lanjutin kerjanya!", reply_markup=MAIN_MENU_KB)
            return

        # 3. CEK LAYAR SEKARANG
        if text == "📸 Cek Layar" or low in ("/layar", "/ss", "layar", "ss", "cek layar"):
            await self._send_screen_snapshot(chat_id)
            return

        # 4. DAFTAR FILE TUGAS
        if text == "📂 Daftar File" or low in ("/file", "/files", "daftar file"):
            flist = self.orchestrator.file_manager.list_files()
            if not flist:
                await self.send_text(chat_id, "📂 Belum ada file di folder tugas bro.", reply_markup=MAIN_MENU_KB)
                return
            kb_rows = [
                [{"text": f"📥 {f['name']} ({f['size']} B)", "callback_data": f"dl_file:{f['name'][:48]}"}]
                for f in flist[:10]
            ]
            await self.send_text(
                chat_id,
                "📂 Daftar File di Workspace (tap buat download ke Telegram):",
                reply_markup={"inline_keyboard": kb_rows},
            )
            return

        # 5. GANTI MODE LEWAT TOMBOL MENU
        if text == "💬 Ngobrol AI" or low == "/ngobrol":
            await self.orchestrator.stop_goal(silent=True)
            self.chat_modes[chat_id] = "chat"
            await self.send_text(
                chat_id,
                "💬 Mode Ngobrol AI Aktif!\n\n"
                "Sekarang lu bebas ngobrol santai, tanya soal, minta bikinin jawaban/esai, atau diskusi apa aja — gua jawab kilat tanpa nunggu browser!",
                reply_markup=MAIN_MENU_KB,
            )
            return

        if text == "📝 Kerjain Tugas Web" or low == "/tugas":
            await self.orchestrator.stop_goal(silent=True)
            self.chat_modes[chat_id] = "agent"
            await self.send_text(
                chat_id,
                "📝 Mode Kerjain Tugas Web Aktif!\n\n"
                "Ketik link/nama web + tugas yang mau dikerjain, contoh:\n"
                "• buka id.wikipedia.org cari Sejarah Indonesia lalu simpan ke tugas.md\n"
                "• kerjakan form / rangkum isi halaman yang sedang terbuka sekarang",
                reply_markup=MAIN_MENU_KB,
            )
            return

        if text == "🌐 Buka Web" or low == "/buka":
            await self.orchestrator.stop_goal(silent=True)
            self.chat_modes[chat_id] = "open_web"
            await self.send_text(
                chat_id,
                "🌐 Mau buka web apa bro?\n\n"
                "Langsung ketik alamat linknya (misal: allindonesia.imigrasi.go.id) ATAU nama layanannya (misal: Google Classroom / Wikipedia):",
                reply_markup=MAIN_MENU_KB,
            )
            return

        # -------------------- EKSEKUSI SESUAI MODE AKTIF --------------------
        mode = self.chat_modes.get(chat_id, "chat")

        # Auto-switch pintar: kalau masih di mode Ngobrol tapi user jelas minta buka web / kasih URL
        import re as _re
        has_url = bool(_re.search(r"(https?://[^\s]+|(?:[a-z0-9\-]+\.)+(?:com|id|co\.id|org|net|go\.id|ac\.id))", low))
        starts_open = low.startswith(("buka web ", "buka situs ", "buka halaman ", "buka link ", "masuk ke web "))
        if mode == "chat" and (starts_open or (has_url and any(w in low for w in ("buka", "cek", "kerjain", "rangkum", "masuk")))):
            if any(w in low for w in ("kerjain", "rangkum", "simpan", "isi", "cari di")):
                mode = "agent"
                self.chat_modes[chat_id] = "agent"
            else:
                clean_q = _re.sub(r"^(buka\s+web|buka\s+situs|buka\s+halaman|buka\s+link|buka)\s+", "", text, flags=_re.I).strip()
                text = clean_q or text
                mode = "open_web"
        if mode == "open_web":
            wait_mid = await self.send_text(chat_id, f"🌐 Lagi ngebukain web '{text}'...")
            target_url = await self.orchestrator.brain.resolve_website_url(text)
            nav_msg = await self.orchestrator.browser.navigate(target_url)
            b64 = await self.orchestrator.browser.refresh_frame()
            # Setelah web terbuka, pindah ke mode agent atau tetap siap menerima instruksi
            self.chat_modes[chat_id] = "agent"
            if wait_mid:
                await self._edit_text(chat_id, wait_mid, f"🌐 {nav_msg}")
            if b64:
                await self.send_photo_b64(
                    chat_id,
                    b64,
                    f"🖥️ {nav_msg}\n\n"
                    "• Kalau mau login/klik sendiri, tap '🖥️ Buka & Kontrol Layar Live'.\n"
                    "• Kalau mau langsung nyuruh gua ngerjain sesuatu di halaman ini, tinggal ketik perintahnya di bawah!",
                    reply_markup=self._live_browser_inline_kb(include_task_btn=False),
                )
            return

        if mode == "chat":
            await self._send_chat_action(chat_id, "typing")
            await self.orchestrator._push_chat("user", text)
            reply = await self.orchestrator.brain.quick_chat(text, self.orchestrator.chat_messages[:-1])
            await self.orchestrator._push_chat("agent", reply)
            await self.send_text(chat_id, reply, reply_markup=MAIN_MENU_KB)
            return

        # Mode "agent" (Kerjain Tugas Web)
        mid = await self.send_text(chat_id, f"⏳ Lagi gua kerjain di browser bro: {text}")
        if mid:
            self._status_msg_ids[chat_id] = mid
        await self.orchestrator.start_goal(text)

    async def _send_screen_snapshot(self, chat_id: int) -> None:
        b64 = await self.orchestrator.browser.refresh_frame()
        if b64:
            cur_url = self.orchestrator.browser.page.url if self.orchestrator.browser.page else ""
            if cur_url.startswith("chrome-error://"):
                await self.orchestrator.browser.navigate("https://id.wikipedia.org")
                b64 = await self.orchestrator.browser.refresh_frame()
                cur_url = self.orchestrator.browser.page.url if self.orchestrator.browser.page else ""
            await self.send_photo_b64(
                chat_id,
                b64,
                f"🖥️ Layar browser saat ini:\n{cur_url}",
                reply_markup=self._live_browser_inline_kb(),
            )
        else:
            await self.send_text(chat_id, "Browser sedang disiapkan...", reply_markup=MAIN_MENU_KB)

    async def _send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        if not self.token:
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"https://api.telegram.org/bot{self.token}/sendChatAction",
                    json={"chat_id": chat_id, "action": action},
                )
        except Exception:
            pass

    async def _edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        if not self.token:
            return
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                await client.post(
                    f"https://api.telegram.org/bot{self.token}/editMessageText",
                    json={"chat_id": chat_id, "message_id": message_id, "text": text[:3900]},
                )
        except Exception:
            pass

    async def _answer_cb(self, cb_id: Optional[str], text: str) -> None:
        if not cb_id or not self.token:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": text},
            )

    async def send_text(self, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> Optional[int]:
        if not self.token:
            return None
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text[:3900]}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=payload)
                d = r.json()
                if d.get("ok"):
                    return d["result"].get("message_id")
        except Exception:
            pass
        return None

    async def notify_step(self, step_num: int, thought: str) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        short_thought = (thought or "").strip()
        if len(short_thought) > 220:
            short_thought = short_thought[:220] + "..."
        new_txt = f"⏳ Langkah {step_num}:\n{short_thought}"
        for cid in list(self.active_chat_ids):
            mid = self._status_msg_ids.get(cid)
            if mid:
                await self._edit_text(cid, mid, new_txt)

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
        caption = f"🔐 Butuh Login Lu Bro\n{reason}"
        rows: List[List[Dict[str, Any]]] = []
        if self.public_web_url.startswith("https://"):
            rows.append([{"text": "🔑 Buka Layar & Ketik Password Aman", "url": self.public_web_url}])
        rows.append([{"text": "✅ Udah Login — Lanjut Kerjain", "callback_data": "resume_login"}])
        markup = {"inline_keyboard": rows}
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
            await self.send_text(cid, f"✅ Beres!\n\n{clean}", reply_markup=MAIN_MENU_KB)

    async def send_to_all(self, message: str) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        for cid in list(self.active_chat_ids):
            await self.send_text(cid, message, reply_markup=MAIN_MENU_KB)
