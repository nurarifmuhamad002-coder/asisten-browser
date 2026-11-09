import asyncio
import base64
import json
import os
from pathlib import Path
import re
import secrets
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

import httpx

from agent.llm_providers import get_shared_http

if TYPE_CHECKING:
    from agent.orchestrator import AgentOrchestrator


MAIN_MENU_KB: Dict[str, Any] = {
    "keyboard": [
        [{"text": "📸 Cek Layar"}, {"text": "📂 Daftar File"}],
        [{"text": "🔒 Keamanan"}, {"text": "⏹️ Stop / Menu"}],
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
    {"command": "kunci", "description": "Kunci Bot & Web Khusus HP Lu"},
    {"command": "stop", "description": "Hentikan Proses Saat Ini"},
]


class TelegramBotBridge:
    """
    Jembatan interaktif ke Telegram di HP user dengan Menu Tombol Langsung + Private Owner Lock:
    1. 💬 Ngobrol AI — Tanya jawab & bantuan tugas instan (<1 detik) tanpa buka browser
    2. 📝 Kerjain Tugas Web — AI mengoperasikan browser multi-langkah + buat file
    3. 🌐 Buka Web — Buka website apa saja + tombol layar live untuk login aman
    4. 🔒 Keamanan (Private Lock) — Kunci bot & layar web live khusus pemilik tunggal
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

        # Private Owner Lock
        raw_owner = os.getenv("TELEGRAM_OWNER_ID", "").strip()
        self.owner_id: Optional[int] = int(raw_owner) if raw_owner.isdigit() else None
        self.owner_label: str = f"ID {self.owner_id}" if self.owner_id else ""
        self.web_access_key: str = os.getenv("WEB_ACCESS_PIN", "").strip() or str(100000 + secrets.randbelow(900000))

        self.active_chat_ids: Set[int] = {self.owner_id} if self.owner_id else set()
        self.chat_modes: Dict[int, str] = {}  # "chat" | "agent" | "open_web"
        self.last_output_text: Dict[int, str] = {}
        self._status_msg_ids: Dict[int, int] = {}
        self._last_rendered_card: Dict[int, str] = {}
        self._run_started_at: float = 0.0
        self._last_step_num: int = 0
        self._last_thought: str = ""
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._seen_update_ids: Set[int] = set()
        self._poll_task: Optional[asyncio.Task] = None
        self._offset: int = 0
        self.bot_username: str = ""

    def attach_orchestrator(self, orchestrator: "AgentOrchestrator") -> None:
        self.orchestrator = orchestrator

    @property
    def is_configured(self) -> bool:
        return bool(self.token)

    @property
    def is_locked(self) -> bool:
        return self.owner_id is not None

    def protected_web_url(self) -> str:
        if not self.public_web_url:
            return ""
        if self.is_locked:
            sep = "&" if "?" in self.public_web_url else "?"
            return f"{self.public_web_url}{sep}k={self.web_access_key}"
        return self.public_web_url

    def public_dict(self) -> Dict[str, Any]:
        return {
            "configured": self.is_configured,
            "bot_username": self.bot_username,
            "mode": "webhook" if self.use_webhook else "polling",
            "running": bool(self.use_webhook or (self._poll_task and not self._poll_task.done())),
            "masked_token": (self.token[:6] + "••••" + self.token[-4:]) if len(self.token) > 12 else "",
            "locked": self.is_locked,
            "owner_id": self.owner_id,
        }

    async def configure(self, token: str, public_web_url: str = "", owner_id: Optional[int] = None) -> Dict[str, Any]:
        await self.stop()
        clean = (token or "").strip()
        if public_web_url:
            self.public_web_url = public_web_url.strip().rstrip("/")
        if owner_id is not None and owner_id > 0:
            self.owner_id = owner_id
            self.active_chat_ids = {owner_id}
        if not clean:
            self.token = ""
            self.bot_username = ""
            return self.public_dict()

        client = get_shared_http()
        resp = await client.get(f"https://api.telegram.org/bot{clean}/getMe", timeout=12.0)
        data = resp.json()
        if not data.get("ok"):
            raise ValueError("Token Bot Telegram tidak valid. Pastikan copy langsung dari @BotFather.")
        self.bot_username = data["result"].get("username", "")

        self.token = clean
        try:
            await client.post(
                f"https://api.telegram.org/bot{clean}/setMyCommands",
                json={"commands": BOT_COMMANDS},
                timeout=10.0,
            )
        except Exception:
            pass

        if self.use_webhook and self.public_web_url.startswith("https://"):
            wh_url = self.public_web_url.rstrip("/") + "/api/telegram/webhook"
            await client.post(
                f"https://api.telegram.org/bot{clean}/setWebhook",
                json={"url": wh_url, "drop_pending_updates": False},
                timeout=12.0,
            )
        else:
            await client.post(f"https://api.telegram.org/bot{clean}/deleteWebhook", timeout=10.0)
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
        live_u = self.protected_web_url()
        if live_u.startswith("https://"):
            rows.append([{"text": "🖥️ Buka & Kontrol Layar Live", "url": live_u}])
        action_row: List[Dict[str, Any]] = [{"text": "🔄 Foto Ulang Layar", "callback_data": "snap_screen"}]
        if include_task_btn:
            action_row.insert(0, {"text": "📝 Kerjain di Web Ini", "callback_data": "mode_agent"})
        rows.append(action_row)
        return {"inline_keyboard": rows}

    def _lock_owner(self, user_id: int, user_obj: Dict[str, Any]) -> None:
        uname = user_obj.get("username")
        fname = user_obj.get("first_name") or "Pemilik"
        self.owner_id = user_id
        self.owner_label = f"{fname} (@{uname})" if uname else f"{fname} (ID: {user_id})"
        # Bersihkan chat_id lain agar notifikasi hanya masuk ke pemilik tunggal
        self.active_chat_ids = {user_id}

    async def _send_security_panel(self, chat_id: int, user_id: int) -> None:
        if self.is_locked:
            live_u = self.protected_web_url()
            kb: Dict[str, Any] = {
                "inline_keyboard": [
                    [{"text": "🔄 Ganti PIN Akses Web Baru", "callback_data": "regen_pin"}],
                    [{"text": "🔓 Buka Kunci Sementara", "callback_data": "unlock_owner"}],
                ]
            }
            if live_u.startswith("https://"):
                kb["inline_keyboard"].insert(0, [{"text": "🖥️ Buka Layar Live (Khusus Pemilik)", "url": live_u}])

            await self.send_text(
                chat_id,
                "🔒 STATUS KEAMANAN: TERKUNCI RAPAT\n\n"
                f"• Pemilik Tunggal: {self.owner_label or self.owner_id}\n"
                f"• Telegram ID Lu: {self.owner_id}\n"
                f"• PIN Web Cadangan: {self.web_access_key}\n\n"
                "Perlindungan aktif:\n"
                "1. Orang lain yang chat bot Telegram ini langsung ditolak otomatis.\n"
                "2. Link web layar browser juga dikunci — orang luar gak bisa ngintip layar lu.\n\n"
                f"💡 Biar permanen selamanya walau server restart, simpan juga di Variables Railway:\n"
                f"TELEGRAM_OWNER_ID = {self.owner_id}",
                reply_markup=kb,
            )
        else:
            kb = {
                "inline_keyboard": [
                    [{"text": "🔒 Kunci Bot & Layar Khusus HP Gua Sekarang", "callback_data": "lock_owner"}]
                ]
            }
            await self.send_text(
                chat_id,
                "⚠️ STATUS KEAMANAN: BELUM DIKUNCI\n\n"
                f"Telegram ID lu terdeteksi: {user_id}\n"
                "Tap tombol di bawah ini buat ngunci bot & layar browser biar cuma HP lu satu-satunya yang bisa ngakses:",
                reply_markup=kb,
            )

    async def _send_main_menu(self, chat_id: int, prefix: str = "") -> None:
        sec_status = f"🔒 Terkunci Khusus HP Lu (ID: {self.owner_id})" if self.is_locked else "⚠️ Belum Dikunci (Tap '🔒 Keamanan')"
        body = (
            f"{prefix}"
            "🤖 Asisten Pintar Otomatis Aktif!\n"
            f"• Keamanan: {sec_status}\n\n"
            "Sekarang lu NGGAK PERLU pencet mode apa-apa dulu — langsung ketik aja bebas di chat:\n"
            "• Mau ngobrol / tanya soal / cek saham & BTC ➔ langsung dijawab kilat!\n"
            "• Mau nyuruh buka web, login, isi form, belanja, atau riset jadi Word/Excel/PDF ➔ robot browser langsung jalan otomatis!\n\n"
            "Tombol bantu cepat di bawah:\n"
            "• 📸 Cek Layar — Intip foto layar browser sekarang\n"
            "• 📂 Daftar File — Ambil / hapus file hasil kerjaan\n"
            "• 🔒 Keamanan — Kunci bot khusus HP lu\n"
            "• ⏹️ Stop / Menu — Hentikan proses kapan aja"
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
            from_user = cb.get("from") or {}
            sender_id = from_user.get("id")
            chat_id = cb.get("message", {}).get("chat", {}).get("id")
            if not chat_id or not sender_id:
                return

            # PRIVATE LOCK GUARD
            if self.is_locked and sender_id != self.owner_id:
                await self._answer_cb(cb_id, "🚫 Akses Ditolak! Bot ini dikunci pemiliknya.")
                return

            if data == "lock_owner":
                self._lock_owner(sender_id, from_user)
                await self._answer_cb(cb_id, "🔒 Berhasil dikunci khusus ke HP lu!")
                await self._send_security_panel(chat_id, sender_id)
                return

            if data == "unlock_owner":
                self.owner_id = None
                self.owner_label = ""
                await self._answer_cb(cb_id, "🔓 Kunci dibuka sementara.")
                await self._send_security_panel(chat_id, sender_id)
                return

            if data == "regen_pin":
                self.web_access_key = str(100000 + secrets.randbelow(900000))
                await self._answer_cb(cb_id, f"PIN baru dibuat: {self.web_access_key}")
                await self._send_security_panel(chat_id, sender_id)
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
                self.start_progress_heartbeat()
                await self._answer_cb(cb_id, "Melanjutkan pekerjaan...")
            elif data == "stop_running":
                await self.orchestrator.stop_goal()
                await self._finalize_progress_cards("⏹️ PEKERJAAN DIHENTIKAN OLEH LU")
                await self._answer_cb(cb_id, "Pekerjaan dihentikan.")
            elif data == "clear_user_mem":
                self.orchestrator.clear_user_memories()
                await self._answer_cb(cb_id, "🗑️ Memori dikosongkan!")
                await self.send_text(chat_id, "🗑️ Semua memori profil sudah dikosongkan.", reply_markup=MAIN_MENU_KB)
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
            elif data == "save_upload_md":
                last_note = self.orchestrator.persistent_notes.get("hasil_baca_terakhir", "")
                if not last_note:
                    await self._answer_cb(cb_id, "Belum ada hasil baca foto/file.")
                    return
                saved = self.orchestrator.file_manager.write_file("hasil_baca_telegram.md", last_note)
                fpath = self.orchestrator.file_manager.get_file_path(saved["name"])
                await self._answer_cb(cb_id, "Mengirim file...")
                await self.send_document(chat_id, fpath.name, fpath.read_bytes(), "📄 Hasil baca berhasil disimpan!")
            elif data == "use_upload_web":
                self.chat_modes[chat_id] = "agent"
                await self._answer_cb(cb_id, "Siap dipakai di browser!")
                cur_u = self.orchestrator.browser.page.url if self.orchestrator.browser.page else ""
                await self.send_text(
                    chat_id,
                    f"📝 Data dari foto/file barusan udah gua pegang!\n"
                    f"Sekarang ketik mau diapain di browser (halaman aktif: {cur_u}), contoh:\n"
                    "• isikan data tadi ke formulir di layar ini\n"
                    "• buka [link web] lalu masukkan data tadi",
                    reply_markup=MAIN_MENU_KB,
                )
            elif data.startswith("exp:"):
                fmt = data.split(":", 1)[1].lower()
                src_txt = self.last_output_text.get(chat_id) or self.orchestrator.persistent_notes.get("hasil_baca_terakhir", "")
                if not src_txt:
                    await self._answer_cb(cb_id, "Belum ada isi teks untuk dijadikan file.")
                    return
                await self._answer_cb(cb_id, f"Menyiapkan file .{fmt.upper()}...")
                from datetime import datetime as _dt
                saved = self.orchestrator.file_manager.export_formatted(f"dokumen_{_dt.now().strftime('%H%M%S')}", src_txt, fmt)
                fpath = self.orchestrator.file_manager.get_file_path(saved["name"])
                await self.send_document(chat_id, fpath.name, fpath.read_bytes(), f"📄 Siap kumpul: {fpath.name}")
            elif data.startswith("dl_file:"):
                fname = data.split(":", 1)[1]
                await self._answer_cb(cb_id, f"Mengirim {fname}...")
                try:
                    fpath = self.orchestrator.file_manager.get_file_path(fname)
                    await self.send_document(chat_id, fpath.name, fpath.read_bytes(), f"📄 File: {fpath.name}")
                except Exception as exc:
                    await self.send_text(chat_id, f"Gagal mengambil file: {exc}")
            elif data.startswith("rm_file:"):
                fname = data.split(":", 1)[1]
                self.orchestrator.file_manager.delete_file(fname)
                await self._answer_cb(cb_id, f"🗑️ {fname} dihapus!")
                await self.send_text(chat_id, f"🗑️ File '{fname}' berhasil dihapus dari Workspace.", reply_markup=MAIN_MENU_KB)
            return

        # -------------------- PHOTO / DOCUMENT / TEXT MESSAGES --------------------
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        from_user = msg.get("from") or {}
        chat_id = chat.get("id")
        sender_id = from_user.get("id") or chat_id
        if not chat_id:
            return

        # PRIVATE LOCK GUARD — Tolak siapa pun selain pemilik tunggal!
        if self.is_locked and sender_id != self.owner_id:
            await self.send_text(
                chat_id,
                "🚫 Akses Ditolak.\nBot & Cloud Browser ini milik pribadi dan sudah dikunci oleh pemiliknya.",
            )
            return

        self.active_chat_ids.add(chat_id)

        # UPGRADE #2 + VOICE NOTE: HANDLE FOTO / GAMBAR / PDF / WORD / EXCEL / SUARA DARI TELEGRAM
        if "photo" in msg or "document" in msg or "voice" in msg or "audio" in msg:
            await self._handle_media_upload(chat_id, msg)
            return

        text = (msg.get("text") or "").strip()
        if not text:
            return

        low = text.lower().strip()
        msg_id = msg.get("message_id")

        # LOGIN SUPER SIMPEL LANGSUNG LEWAT BALASAN CHAT TELEGRAM (Tanpa perlu buka layar web!)
        menu_or_cmd = text in (
            "💬 Ngobrol AI", "📝 Kerjain Tugas Web", "🌐 Buka Web", "📸 Cek Layar",
            "📂 Daftar File", "🔒 Keamanan", "⏹️ Stop / Menu",
        ) or low.startswith("/") or low in ("stop", "batal", "menu", "kunci")
        if self.orchestrator.state == "WAITING_HUMAN_TAKEOVER" and not menu_or_cmd:
            # Langsung hapus pesan berisi password dari riwayat chat Telegram dalam <0.5 detik agar 100% aman & bersih!
            if msg_id:
                asyncio.create_task(self._delete_msg(chat_id, msg_id))
            login_res = await self.orchestrator.browser.smart_login_from_chat(
                text,
                captcha_solver=self.orchestrator.brain.solve_captcha_image,
            )
            await self.send_text(
                chat_id,
                f"🔐 {login_res}\n🧹 Pesan sandi barusan otomatis gua hapus dari chat biar aman. Lanjut kerjain tugas!",
                reply_markup=MAIN_MENU_KB,
            )
            await self.orchestrator.resume_from_takeover()
            self.start_progress_heartbeat()
            return

        # PELINDUNG SAAT BOT SEDANG KERJA AKTIF: Jangan batalkan tugas kalau user cuma nanya kabar ("?", "udah belum", dll)!
        if self.orchestrator.state == "RUNNING" and not menu_or_cmd:
            check_phrases = (
                "?", "udah", "belum", "gimana", "kok lama", "lama", "jalan gak", "lagi ngerjain",
                "masih", "halo", "bro", "tes", "cek", "nyangkut", "aman",
            )
            if len(text) <= 32 and any(cp in low for cp in check_phrases):
                import time as _t
                el_s = max(1, int(_t.monotonic() - self._run_started_at)) if self._run_started_at else 1
                await self.send_text(
                    chat_id,
                    f"🟢 Mesin masih jalan aktif bro (⏱️ {el_s} detik berjalan)!\n"
                    f"📍 Lagi di: {self.orchestrator.live_status}\n\n"
                    "Tenang aja, tugas lu nggak gua batalin. Tap 📸 Cek Layar kalau mau ngintip posisinya, atau ketik `stop` kalau beneran mau batalin!",
                    reply_markup=MAIN_MENU_KB,
                )
                return

        # PENGATURAN AI & PROXY INDONESIA LANGSUNG DARI TELEGRAM (/setting, /apikey, /proxy)
        if low in ("/setting", "/settings", "setting"):
            llm_info = self.orchestrator.llm_config.public_dict()
            px_stat = self.orchestrator.browser.proxy_summary()
            mem_cnt = len(self.orchestrator.user_memories)
            await self.send_text(
                chat_id,
                "⚙️ PENGATURAN ASISTEN LU SEKARANG:\n\n"
                f"• Otak AI: {llm_info.get('mode')}\n"
                f"• Kunci AI: {llm_info.get('masked_key') or '(belum ada)'}\n"
                f"• Memori Profil: {mem_cnt} data tersimpan (/ingat)\n"
                f"• Jalur IP Browser: {px_stat}\n"
                f"• Keamanan: {'🔒 Terkunci' if self.is_locked else '🔓 Terbuka'}\n\n"
                "🧠 Lihat/tambah data profil yang dihafal bot:\n"
                "/ingat\n\n"
                "🇮🇩 Cara pasang IP Indonesia (buat tembus web .go.id):\n"
                "/proxy host:port:username:password\n\n"
                "Cara ganti API Key Gemini:\n"
                "/apikey KODE_API_KEY_BARU\n\n"
                "Cara segarin ulang browser:\n"
                "/reset",
                reply_markup=MAIN_MENU_KB,
            )
            return

        # FITUR MEMORI PROFIL & GAYA KERJA OTOMATIS (/ingat + bahasa santai "ingat ya..." / "hafalin...")
        natural_mem_match = re.match(
            r"^(?:tolong\s+|bro\s+|eh\s+)?(?:ingat(?:\s+ya)?|ingetin|hafalin(?:\s+ya)?|simpan\s+(?:data|profil|memori)(?:\s+gua)?|catat\s+(?:data|profil)(?:\s+gua)?)\s*[:,\-]?\s*(.+)$",
            text.strip(),
            flags=re.I | re.S,
        )
        if low == "/ingat" or low.startswith("/ingat ") or natural_mem_match:
            arg = text[6:].strip() if (low == "/ingat" or low.startswith("/ingat ")) else natural_mem_match.group(1).strip()
            arg_low = arg.lower()

            if not arg or arg_low in ("list", "cek", "lihat"):
                mems = self.orchestrator.user_memories
                if not mems:
                    await self.send_text(
                        chat_id,
                        "🧠 MEMORI PROFIL & GAYA KERJA LU MASIH KOSONG!\n\n"
                        "Cara nyimpannya gampang banget, tinggal ketik bebas (bisa 1 baris atau banyak baris sekaligus):\n\n"
                        "Contoh:\n"
                        "`/ingat Nama Lengkap: Muhamad Nur Arif\n"
                        "Email: arif@gmail.com\n"
                        "No HP: 081234567890\n"
                        "Kantor: PT Travel Umrah ...\n"
                        "Alamat: Jakarta`\n\n"
                        "Atau ketik santai aja di chat: *'ingat ya bro nama lengkap gua ..., email gua ...'*\n"
                        "Nanti tiap disuruh **isi formulir web** atau **bikin surat/dokumen Word/Excel/PDF**, otomatis gua pake data itu tanpa nanya ulang!",
                        reply_markup=MAIN_MENU_KB,
                    )
                else:
                    lines = "\n".join(f"{i}. {m}" for i, m in enumerate(mems, 1))
                    kb_mem = {
                        "inline_keyboard": [
                            [{"text": "🗑️ Hapus Semua Memori", "callback_data": "clear_user_mem"}]
                        ]
                    }
                    await self.send_text(
                        chat_id,
                        f"🧠 DATA PROFIL & GAYA KERJA YANG GUA HAFAL ({len(mems)} item):\n\n"
                        f"{lines}\n\n"
                        "• Tambah/ubah data: `/ingat Email: emailbaru@gmail.com`\n"
                        "• Hapus 1 baris: `/ingat hapus 2`",
                        reply_markup=kb_mem,
                    )
                return

            if arg_low in ("reset", "hapus semua", "clear", "kosongkan"):
                self.orchestrator.clear_user_memories()
                await self.send_text(chat_id, "🗑️ Sip, semua memori profil lu udah dikosongkan bersih!", reply_markup=MAIN_MENU_KB)
                return

            del_m = re.match(r"^(?:hapus|del|delete|buang)\s+(\d+)$", arg_low)
            if del_m:
                removed = self.orchestrator.remove_user_memory(int(del_m.group(1)))
                if removed:
                    await self.send_text(chat_id, f"🗑️ Dihapus dari memori: `{removed}`", reply_markup=MAIN_MENU_KB)
                else:
                    await self.send_text(chat_id, "Nomor urut memori nggak ketemu bro. Ketik `/ingat` buat lihat daftarnya!", reply_markup=MAIN_MENU_KB)
                return

            saved_items = self.orchestrator.add_user_memories(arg)
            recap = "\n".join(f"• {x}" for x in saved_items)
            await self.send_text(
                chat_id,
                f"🧠 Sip bro, udah masuk ke otak gua ({len(self.orchestrator.user_memories)} total memori tersimpan):\n\n"
                f"{recap}\n\n"
                "Mulai sekarang tiap lu nyuruh **isi form di web** (*'isi form ini pake data gua'*) atau **bikin surat/dokumen**, langsung otomatis gua pake! Ketik `/ingat` kapan aja buat ngecek.",
                reply_markup=MAIN_MENU_KB,
            )
            return

        if low == "/proxy" or low.startswith("/proxy "):
            arg = text[6:].strip() if len(text) > 6 else ""
            if not arg or arg.lower() in ("cek", "status", "info", "ip"):
                wait_id = await self.send_text(chat_id, "🌐 Mengecek lokasi IP jalur browser sekarang...")
                ip_now = await self.orchestrator.browser.check_exit_ip()
                px_stat = self.orchestrator.browser.proxy_summary()
                msg_px = (
                    "🇮🇩 STATUS JALUR IP BROWSER:\n\n"
                    f"• Mode Proxy: {px_stat}\n"
                    f"• IP Keluar Saat Ini: {ip_now}\n\n"
                    "💡 Cara Aktifkan IP Indonesia (tembus Siskopatuh / web .go.id):\n"
                    "Tinggal tempel kode proxy dari penyedia Proxy Residential Indo (format apa aja langsung kebaca otomatis):\n"
                    "  `/proxy host:port:user:pass`\n"
                    "  atau `/proxy http://user:pass@host:port`\n"
                    "  atau `/proxy socks5://user:pass@host:port`\n\n"
                    "🔌 Cara matikan proxy (balik gratis Singapore):\n"
                    "  `/proxy off`"
                )
                if wait_id:
                    await self._edit_text(chat_id, wait_id, msg_px)
                else:
                    await self.send_text(chat_id, msg_px, reply_markup=MAIN_MENU_KB)
                return

            # Hapus pesan berisi kredensial proxy dari riwayat chat biar aman!
            if msg_id and arg.lower() not in ("off", "mati", "hapus", "none", "reset", "matikan"):
                asyncio.create_task(self._delete_msg(chat_id, msg_id))

            wait_id = await self.send_text(chat_id, "🔄 Memasang jalur jaringan baru ke browser & mengecek lokasi IP...")
            try:
                await self.orchestrator.stop_goal(silent=True)
                res_px = await self.orchestrator.browser.configure_proxy(arg)
                if res_px.get("enabled"):
                    done_txt = (
                        "✅ JALUR PROXY BERHASIL DIPASANG!\n"
                        "🧹 Pesan kode proxy lu tadi otomatis gua hapus dari chat biar aman.\n\n"
                        f"• Server Proxy: {res_px.get('server')}\n"
                        f"• Lokasi IP Browser Sekarang: {res_px.get('ip_info')}\n\n"
                        "(Kuota super irit: cuma halaman browser yang lewat proxy, iklan/video otomatis diblokir, dan AI Gemini nggak makan kuota proxy!).\n"
                        "Gas langsung ketik perintah buka Siskopatuh atau web apa aja!"
                    )
                else:
                    done_txt = (
                        "🔌 Proxy dimatikan — Browser balik ke jalur gratis Cloud Singapore!\n"
                        f"• Lokasi IP Sekarang: {res_px.get('ip_info')}"
                    )
                if wait_id:
                    await self._edit_text(chat_id, wait_id, done_txt)
                else:
                    await self.send_text(chat_id, done_txt, reply_markup=MAIN_MENU_KB)
            except Exception as px_exc:
                err_txt = f"⚠️ Gagal memasang proxy: {px_exc}"
                if wait_id:
                    await self._edit_text(chat_id, wait_id, err_txt)
                else:
                    await self.send_text(chat_id, err_txt, reply_markup=MAIN_MENU_KB)
            return

        if low.startswith("/apikey "):
            new_k = text.split(None, 1)[1].strip()
            self.orchestrator.llm_config.update("gemini", "gemini-3.1-flash-lite", new_k, "")
            await self.send_text(chat_id, "✅ Mantap! API Key Gemini baru udah langsung aktif!", reply_markup=MAIN_MENU_KB)
            return

        if low in ("/reset", "reset browser"):
            await self.orchestrator.stop_goal(silent=True)
            await self.orchestrator.browser.stop()
            await self.orchestrator.browser.ensure_alive()
            await self.send_text(chat_id, "🔄 Browser server berhasil di-refresh bersih!", reply_markup=MAIN_MENU_KB)
            return

        # 0. MENU KEAMANAN / KUNCI PRIBADI
        if text == "🔒 Keamanan" or low in ("/kunci", "kunci", "/id", "/lock"):
            if low in ("/kunci", "kunci", "/lock") and not self.is_locked:
                self._lock_owner(sender_id, from_user)
            await self._send_security_panel(chat_id, sender_id)
            return

        # 1. START / MENU / STOP
        if low in ("/start", "/menu", "menu"):
            self.chat_modes[chat_id] = "auto"
            await self._send_main_menu(chat_id, "Halo bro! 👋 ")
            return

        if text == "⏹️ Stop / Menu" or low in ("/stop", "stop", "berhenti", "batal"):
            await self.orchestrator.stop_goal(silent=True)
            self.chat_modes[chat_id] = "auto"
            await self._send_main_menu(chat_id, "⏹️ Proses yang lagi jalan udah dihentikan.\n\n")
            return

        # 2. LANJUT SETELAH LOGIN (Hanya jika memang sedang menunggu takeover login!)
        if low in ("/lanjut", "udah login", "sudah login") or (low == "lanjut" and self.orchestrator.state == "WAITING_HUMAN_TAKEOVER"):
            await self.orchestrator.resume_from_takeover()
            await self.send_text(chat_id, "✅ Oke sip, gua lanjutin kerjanya!", reply_markup=MAIN_MENU_KB)
            return

        # 3. CEK LAYAR SEKARANG
        if text == "📸 Cek Layar" or low in ("/layar", "/ss", "layar", "ss", "cek layar"):
            await self._send_screen_snapshot(chat_id)
            return

        # 4. DAFTAR & KELOLA FILE TUGAS
        if text == "📂 Daftar File" or low in ("/file", "/files", "daftar file"):
            flist = self.orchestrator.file_manager.list_files()
            if not flist:
                await self.send_text(chat_id, "📂 Belum ada file di folder tugas bro.", reply_markup=MAIN_MENU_KB)
                return
            kb_rows = [
                [
                    {"text": f"📥 {f['name'][:26]} ({f['size']} B)", "callback_data": f"dl_file:{f['name'][:48]}"},
                    {"text": "🗑️ Hapus", "callback_data": f"rm_file:{f['name'][:48]}"},
                ]
                for f in flist[:10]
            ]
            await self.send_text(
                chat_id,
                "📂 Kelola File di Workspace (tap 📥 buat download atau 🗑️ buat hapus):",
                reply_markup={"inline_keyboard": kb_rows},
            )
            return

        # Backward compatibility bila tombol/command lama ditekan
        if text in ("💬 Ngobrol AI", "📝 Kerjain Tugas Web") or low in ("/ngobrol", "/tugas"):
            self.chat_modes[chat_id] = "auto"
            await self.send_text(
                chat_id,
                "⚡ Sekarang udah 100% Otomatis bro — nggak perlu pencet mode dulu! Langsung ketik aja mau ngobrol atau mau nyuruh kerjain tugas web apa!",
                reply_markup=MAIN_MENU_KB,
            )
            return

        if text == "🌐 Buka Web" or low == "/buka":
            await self.orchestrator.stop_goal(silent=True)
            self.chat_modes[chat_id] = "open_web"
            await self.send_text(
                chat_id,
                "🌐 Langsung ketik alamat link atau nama website-nya bro:",
                reply_markup=MAIN_MENU_KB,
            )
            return

        # ==================== AUTO-ROUTER PINTAR (TANPA PENCET MODE DULU!) ====================
        mode, routed_text = await self._detect_auto_route(chat_id, text, low)
        text = routed_text

        if mode == "open_web":
            import time as _t
            t_open = _t.monotonic()
            asyncio.create_task(self._send_chat_action(chat_id, "typing"))
            wait_mid = await self.send_text(
                chat_id,
                f"🟢 MESIN SEDANG MEMBUKA WEB · ⏱️ 0 dtk\n🎯 Tujuan: {text}\n📍 Aktivitas: Mencari alamat resmi & memuat halaman...",
            )
            target_url = await self.orchestrator.brain.resolve_website_url(text)
            asyncio.create_task(self._send_chat_action(chat_id, "upload_photo"))
            nav_msg = await self.orchestrator.browser.navigate(target_url)
            b64 = await self.orchestrator.browser.refresh_frame()
            self.chat_modes[chat_id] = "auto"
            el_open = max(1, int(_t.monotonic() - t_open))
            if wait_mid:
                await self._edit_text(
                    chat_id,
                    wait_mid,
                    f"✅ SELESAI DIBUKA ({el_open} dtk)!\n🌐 {nav_msg}",
                )
            if b64:
                await self.send_photo_b64(
                    chat_id,
                    b64,
                    f"🖥️ {nav_msg}\n\n"
                    "Tinggal ketik aja langsung di chat kalau mau nyuruh gua klik/isi/cari/rangkum sesuatu di halaman ini!",
                    reply_markup=self._live_browser_inline_kb(include_task_btn=False),
                )
            return

        if mode == "chat":
            # Deteksi format file yang diminta user (Word / Excel / PDF) secara fleksibel
            req_fmt = ""
            if re.search(r"\b(pdf|\.pdf)\b", low):
                req_fmt = "pdf"
            elif re.search(r"\b(excel|xlsx|\.xlsx|spreadsheet)\b", low):
                req_fmt = "xlsx"
            elif re.search(r"\b(word|docx|\.docx)\b", low):
                req_fmt = "docx"

            # KASUS A: User minta KONVERSI / KIRIM ULANG file yang barusan dibuat (contoh: "ke pdf", "jadiin excel", "mana pdf nama gua nya?")
            is_followup_convert = bool(
                req_fmt
                and (
                    len(low.split()) <= 6
                    or any(
                        p in low
                        for p in (
                            "ke pdf", "ke word", "ke excel", "jadi pdf", "jadi word", "jadi excel",
                            "jadiin pdf", "jadiin word", "jadiin excel", "ubah ke", "konversi", "convert",
                            "mana pdf", "mana word", "mana excel", "mana file", "kirim pdf", "kirim file",
                            "versi pdf", "versi word", "versi excel",
                        )
                    )
                )
                and not any(
                    new_w in low
                    for new_w in ("bikinin surat", "buatkan surat", "buatkan makalah", "tulis artikel", "jelaskan ", "apa bedanya ")
                )
            )
            if is_followup_convert:
                asyncio.create_task(self._send_chat_action(chat_id, "upload_document"))
                fm = self.orchestrator.file_manager
                user_files = [
                    f for f in fm.list_files()
                    if f["name"] not in ("profil_pengajuan.json", "catatan_workspace.md")
                ]
                src_content = ""
                stem_name = ""
                # Cek apakah user menyebut nama file spesifik, atau ambil file terbaru di workspace
                for uf in user_files:
                    st = Path(uf["name"]).stem.lower()
                    if st and st in low:
                        stem_name = Path(uf["name"]).stem
                        src_content = fm.read_file(uf["name"])
                        break
                if not src_content and user_files:
                    latest_f = user_files[0]["name"]
                    stem_name = Path(latest_f).stem
                    if Path(latest_f).suffix.lower().lstrip(".") == req_fmt:
                        fpath = fm.get_file_path(latest_f)
                        await self.send_document(chat_id, fpath.name, fpath.read_bytes(), f"📄 Ini file {fpath.name} lu bro!")
                        return
                    src_content = fm.read_file(latest_f)
                if not src_content:
                    src_content = self.last_output_text.get(chat_id) or ""
                if not src_content and self.orchestrator.user_memories:
                    src_content = "\n".join(self.orchestrator.user_memories)
                    stem_name = "data_profil"

                if src_content.strip():
                    from datetime import datetime as _dt
                    out_stem = stem_name or f"dokumen_{_dt.now().strftime('%H%M%S')}"
                    saved = fm.export_formatted(out_stem, src_content, req_fmt)
                    fpath = fm.get_file_path(saved["name"])
                    await self.send_document(
                        chat_id,
                        fpath.name,
                        fpath.read_bytes(),
                        f"📄 Beres dikonversi jadi .{req_fmt.upper()} asli: {fpath.name}",
                    )
                    return

            # KASUS B: Ngobrol biasa ATAU minta dibuatkan dokumen baru (Word / Excel / PDF)
            asyncio.create_task(self._send_chat_action(chat_id, "upload_document" if req_fmt else "typing"))
            await self.orchestrator._push_chat("user", text)
            prompt_for_ai = (
                f"{text}\n\n(INSTRUKSI SISTEM: Tulis HANYA isi naskah/tabel dokumennya saja secara bersih dan lengkap tanpa kata pengantar obrolan dan TANPA link download palsu, karena sistem otomatis mengubah teks balasanmu menjadi file .{req_fmt.upper()} asli!)"
                if req_fmt and not any(q in low for q in ("apa itu ", "bedanya ", "cara buka "))
                else text
            )
            reply = await self.orchestrator.brain.quick_chat(
                prompt_for_ai,
                self.orchestrator.chat_messages[:-1],
                user_memory_block=self.orchestrator.format_user_memory_block(),
            )
            # Sapu bersih kalau-kalau AI masih menulis link halusinasi sandbox:/mnt/data/...
            reply = re.sub(r"\[[^\]]*\]\(sandbox:[^\)]+\)", "", reply).strip()
            await self.orchestrator._push_chat("agent", reply)
            self.last_output_text[chat_id] = reply

            if req_fmt and not any(q in low for q in ("apa itu pdf", "apa itu excel", "apa itu word")):
                from datetime import datetime as _dt
                m_fn = re.search(r"([a-zA-Z0-9_\-]{2,40})\.(?:pdf|docx|xlsx)\b", text)
                stem_new = m_fn.group(1) if m_fn else f"dokumen_{_dt.now().strftime('%H%M%S')}"
                saved = self.orchestrator.file_manager.export_formatted(stem_new, reply, req_fmt)
                fpath = self.orchestrator.file_manager.get_file_path(saved["name"])
                await self.send_document(chat_id, fpath.name, fpath.read_bytes(), f"📄 File .{req_fmt.upper()} siap dipakai: {fpath.name}")
                return

            if len(reply) >= 90:
                export_kb = {
                    "inline_keyboard": [
                        [
                            {"text": "📘 Word (.docx)", "callback_data": "exp:docx"},
                            {"text": "📗 Excel (.xlsx)", "callback_data": "exp:xlsx"},
                            {"text": "📕 PDF (.pdf)", "callback_data": "exp:pdf"},
                        ]
                    ]
                }
                await self.send_text(chat_id, reply, reply_markup=export_kb)
            else:
                await self.send_text(chat_id, reply, reply_markup=MAIN_MENU_KB)
            return

        # Mode "agent" (Kerjain Tugas Web)
        import time as _t
        self._run_started_at = _t.monotonic()
        self._last_step_num = 1
        self._last_thought = "Memahami perintah & menyiapkan browser..."
        init_card = (
            "🟢 MESIN SEDANG BEKERJA AKTIF · ⏱️ 0 dtk\n"
            f"🎯 Tugas: {text[:130]}\n"
            "📍 Aktivitas: Memahami perintah & menyiapkan browser..."
        )
        mid = await self.send_text(chat_id, init_card, reply_markup=self._running_inline_kb())
        if mid:
            self._status_msg_ids[chat_id] = mid
            self._last_rendered_card[chat_id] = init_card
        await self.orchestrator.start_goal(text)
        self.start_progress_heartbeat()

    async def _detect_auto_route(self, chat_id: int, text: str, low: str) -> tuple[str, str]:
        """
        Router otomatis pintar 2-lapis (0ms aturan pasti + <0.4s AI untuk kalimat abu-abu)
        supaya user CUKUP KETIK BEBAS tanpa perlu menekan tombol mode apa pun terlebih dahulu!
        """
        # 1. Perintah "lanjut" / "terusin" dari pekerjaan browser sebelumnya
        if low in ("lanjut", "lanjut bro", "lanjutin", "terusin", "lanjutkan", "gas lanjut") and self.orchestrator.current_goal:
            return (
                "agent",
                f"Lanjutkan tugas ini sampai tuntas dari posisi halaman browser yang sedang terbuka sekarang: {self.orchestrator.current_goal}",
            )

        has_url = bool(
            re.search(
                r"(https?://[^\s]+|(?:[a-z0-9\-]+\.)+(?:com|id|co\.id|org|net|go\.id|ac\.id|sa|io|app|edu|gov))",
                low,
            )
        )
        task_verbs = (
            " lalu ", " terus ", " setelah itu ", " kemudian ", "isi ", "isiin", "klik", "pencet",
            "submit", "verifikasi", "rangkum", "catat", "kumpulkan", "ambil ", "simpan ke", "buatkan laporan",
            "buatkan rekap", "masukkan ", "checkout", "keranjang", "login", "sign in", "daftar ", "booking",
            "pesan ", "tunggu gua login", "first name", "username:", "password:", "kerjakan tugas",
        )
        has_task_verb = any(tv in low for tv in task_verbs)

        # 2. Jika baru saja menekan tombol eksplisit "/buka" atau "🌐 Buka Web"
        if self.chat_modes.get(chat_id) == "open_web":
            self.chat_modes[chat_id] = "auto"
            if has_task_verb:
                return ("agent", text)
            return ("open_web", text)

        # 3. Jelas Tugas Multi-Langkah Browser (0ms — langsung jalan tanpa delay!)
        known_sites = (
            "wikipedia", "classroom", "google drive", "youtube", "github", "konami", "efhub", "efootball",
            "pesdb", "futbin", "siskopatuh", "shopee", "tokopedia", "traveloka", "tiket", "demoqa",
            "saucedemo", "testpages", "toscrape", "kompas", "detik", "cnbc", "elearning", "e-learning",
            "kampus", "portal", "gmail", "instagram", "linkedin",
        )
        mentions_site = has_url or any(ks in low for ks in known_sites) or any(
            w in low for w in ("di web ", "ke web ", "di situs ", "ke situs ", "dari web ", "dari situs ")
        )
        direct_page_actions = (
            "halaman ini", "di web ini", "di layar", "web nya", "webnya", "situsnya",
            "masuk ke web", "ke web nya", "ke webnya", "isi form", "formulir",
        )

        # Deteksi frasa perintah buka website walaupun diawali kata santai ("coba buka efhub bro", "tolong bukain ...", "bro buka ...")
        non_web_buka = ("buka puasa", "buka bersama", "bukber", "buka rekening", "buka usaha", "buka toko", "jam buka")
        open_match = (
            None
            if any(nw in low for nw in non_web_buka) or any(q in low for q in ("cara buka ", "gimana cara buka ", "syarat buka "))
            else re.match(
                r"^(?:halo\s+|eh\s+|bro\s+|bang\s+|tolong\s+|coba\s+|bantu\s+|bisa\s+|gas\s+)*"
                r"(?:buka(?:in|kan)?|masuk(?:\s+ke)?|kunjungi)\s+(?:web\s+|website\s+|situs\s+|link\s+|halaman\s+)?(.+)$",
                text.strip(),
                flags=re.I,
            )
        )

        if (mentions_site and (has_task_verb or any(w in low for w in ("cari ", "cek ", "baca ", "lihat ", "liat ", "rekap")))) or any(
            dp in low for dp in direct_page_actions
        ):
            return ("agent", text)

        if open_match and (has_task_verb or any(w in low for w in (" lalu ", " terus ", " cari ", " cek ", " baca ", " rangkum"))):
            return ("agent", text)

        if any(low.startswith(p) for p in ("klik ", "pencet ", "isi ", "ketik ", "scroll ", "geser ", "pilih ")):
            return ("agent", text)

        # 4. Minta bukain 1 website/layanan apa saja tanpa tugas lanjutan (contoh: "coba buka efhub bro", "buka youtube.com")
        words = low.split()
        is_finance_word = any(fw in low for fw in ("saham", "harga", "etf", "kurs", "bitcoin", "btc", "kripto", "crypto", "ihsg", "emas"))

        def _clean_site_target(raw_t: str) -> str:
            t = re.sub(
                r"^(?:halo\s+|eh\s+|bro\s+|bang\s+|tolong\s+|coba\s+|bantu\s+|bisa\s+|gas\s+)*"
                r"(?:buka(?:in|kan)?|masuk(?:\s+ke)?|kunjungi)\s*(?:web\s+|website\s+|situs\s+|link\s+|halaman\s+)?",
                "",
                raw_t.strip(),
                flags=re.I,
            ).strip()
            t = re.sub(r"\s+(?:bro|bang|dong|ya|yah|nih|deh|plis|please|sekarang|dulu|aja)+[\?\!\.]*$", "", t, flags=re.I).strip()
            return t

        if not is_finance_word and not has_task_verb:
            if has_url and len(words) <= 5:
                clean_u = _clean_site_target(text)
                return ("open_web", clean_u or text)
            if open_match and len(words) <= 7:
                clean_u = _clean_site_target(text)
                if clean_u and clean_u.lower() not in ("bro", "dong", "ya", "ini", "itu"):
                    return ("open_web", clean_u)

        # 5. Jelas Ngobrol / Harga Saham & Kripto / Soal / Bikin Surat Tanpa Buka Website (0ms!)
        browser_hint_words = (
            "web", "situs", "browser", "link", "url", "akun ", "halaman",
            "buka", "bukain", "masuk", "cariin", "cekin", "liatin", "login",
        )
        obvious_chat = (
            is_finance_word
            or (
                not mentions_site
                and not open_match
                and not has_task_verb
                and not any(w in low for w in browser_hint_words)
            )
        )
        if obvious_chat:
            return ("chat", text)

        # 6. Kalimat Abu-Abu / Bahasa Gaul Bebas -> Tanya AI Classifier Kilat (<0.4 detik)
        cur_u = ""
        try:
            if self.orchestrator.browser.page:
                cur_u = self.orchestrator.browser._display_url(self.orchestrator.browser.page.url)
        except Exception:
            pass
        ai_route = await self.orchestrator.brain.classify_intent(
            text,
            current_url=cur_u,
            recent_chat=self.orchestrator.chat_messages[-4:],
        )
        if ai_route == "open_web":
            clean_u = _clean_site_target(text)
            return ("open_web", clean_u or text)
        return (ai_route, text)

    async def _download_telegram_file(self, file_id: str) -> bytes:
        client = get_shared_http()
        r_meta = await client.get(
            f"https://api.telegram.org/bot{self.token}/getFile",
            params={"file_id": file_id},
            timeout=15.0,
        )
        meta = r_meta.json()
        if not meta.get("ok"):
            raise RuntimeError("Gagal mengambil info file dari Telegram.")
        file_path = meta["result"]["file_path"]
        r_raw = await client.get(f"https://api.telegram.org/file/bot{self.token}/{file_path}", timeout=30.0)
        r_raw.raise_for_status()
        return r_raw.content

    async def _handle_media_upload(self, chat_id: int, msg: Dict[str, Any]) -> None:
        from datetime import datetime as _dt

        caption = (msg.get("caption") or "").strip()
        file_id = ""
        fname = ""
        mime_type = "application/octet-stream"

        if "photo" in msg and msg["photo"]:
            best_photo = msg["photo"][-1]
            file_id = best_photo.get("file_id", "")
            fname = f"foto_{_dt.now().strftime('%H%M%S')}.jpg"
            mime_type = "image/jpeg"
        elif "voice" in msg and msg["voice"]:
            vc = msg["voice"]
            file_id = vc.get("file_id", "")
            fname = f"suara_{_dt.now().strftime('%H%M%S')}.ogg"
            mime_type = vc.get("mime_type") or "audio/ogg"
        elif "audio" in msg and msg["audio"]:
            au = msg["audio"]
            file_id = au.get("file_id", "")
            fname = au.get("file_name") or f"audio_{_dt.now().strftime('%H%M%S')}.mp3"
            mime_type = au.get("mime_type") or "audio/mpeg"
        elif "document" in msg and msg["document"]:
            doc = msg["document"]
            file_id = doc.get("file_id", "")
            fname = doc.get("file_name") or f"dokumen_{_dt.now().strftime('%H%M%S')}"
            mime_type = doc.get("mime_type") or "application/octet-stream"

        if not file_id:
            return

        wait_mid = await self.send_text(chat_id, f"📥 Lagi baca '{fname}' kiriman lu bro...")
        try:
            raw_bytes = await self._download_telegram_file(file_id)
            saved = self.orchestrator.file_manager.save_bytes(fname, raw_bytes)
            saved_name = saved["name"]

            extracted_text = ""
            if not mime_type.startswith("image/"):
                extracted_text = self.orchestrator.file_manager.read_file(saved_name, max_chars=20000)
                if extracted_text.startswith("[File Biner:") or extracted_text.startswith("[Gagal"):
                    extracted_text = ""

            analysis = await self.orchestrator.brain.analyze_media(
                user_prompt=caption,
                filename=saved_name,
                raw_bytes=raw_bytes,
                mime_type=mime_type,
                extracted_text=extracted_text,
            )

            # Simpan ke ingatan permanen supaya bisa langsung dipakai di chat maupun di browser!
            combined_note = f"File: {saved_name}\nHasil Baca:\n{analysis}"
            if extracted_text:
                combined_note += f"\n\nTeks Asli:\n{extracted_text[:4000]}"
            self.orchestrator.persistent_notes["hasil_baca_terakhir"] = combined_note
            self.last_output_text[chat_id] = analysis
            await self.orchestrator._push_chat("user", f"[Kirim {saved_name}] {caption}".strip())
            await self.orchestrator._push_chat("agent", analysis)

            if wait_mid:
                await self._edit_text(chat_id, wait_mid, f"✅ File '{saved_name}' berhasil dibaca & disimpan ke Workspace!")

            cap_low = caption.lower()
            wants_web_exec = any(
                kw in cap_low
                for kw in ("isiin ke web", "isi ke web", "masukin ke web", "isi form", "buka ", "kerjain di web")
            )
            if wants_web_exec:
                mid = await self.send_text(chat_id, f"⏳ Lanjut ngerjain ke browser pakai data dari '{saved_name}'...")
                if mid:
                    self._status_msg_ids[chat_id] = mid
                await self.orchestrator.start_goal(
                    f"{caption} (Gunakan data hasil baca dari file '{saved_name}' yang sudah ada di catatan memori)."
                )
                return

            kb = {
                "inline_keyboard": [
                    [{"text": "📝 Pakai Data Ini Buat Isi / Kerjain Web", "callback_data": "use_upload_web"}],
                    [
                        {"text": "📘 Word", "callback_data": "exp:docx"},
                        {"text": "📗 Excel", "callback_data": "exp:xlsx"},
                        {"text": "📕 PDF", "callback_data": "exp:pdf"},
                    ],
                ]
            }
            await self.send_text(chat_id, analysis, reply_markup=kb)

        except Exception as exc:
            await self.send_text(chat_id, f"⚠️ Gagal memproses file '{fname}': {exc}", reply_markup=MAIN_MENU_KB)

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
            await get_shared_http().post(
                f"https://api.telegram.org/bot{self.token}/sendChatAction",
                json={"chat_id": chat_id, "action": action},
                timeout=4.0,
            )
        except Exception:
            pass

    def _running_inline_kb(self) -> Dict[str, Any]:
        rows: List[List[Dict[str, Any]]] = [
            [
                {"text": "📸 Intip Layar", "callback_data": "snap_screen"},
                {"text": "⏹️ Hentikan", "callback_data": "stop_running"},
            ]
        ]
        live_u = self.protected_web_url()
        if live_u.startswith("https://"):
            rows.append([{"text": "🖥️ Tonton Layar Live (CCTV)", "url": live_u}])
        return {"inline_keyboard": rows}

    def _build_progress_card_text(self, header_override: str = "", tick_idx: int = 0) -> str:
        import time as _t
        el_s = max(1, int(_t.monotonic() - self._run_started_at)) if self._run_started_at else 1
        pulse = ("🟢", "⚡")[tick_idx % 2]
        hdr = header_override or f"{pulse} MESIN SEDANG BEKERJA AKTIF · ⏱️ {el_s} dtk"

        cur_u = ""
        try:
            if self.orchestrator and self.orchestrator.browser and self.orchestrator.browser.page:
                raw_u = self.orchestrator.browser._display_url(self.orchestrator.browser.page.url)
                if raw_u and raw_u != "about:blank":
                    cur_u = raw_u[:75]
        except Exception:
            pass

        plan = (self.orchestrator.current_plan if self.orchestrator else None) or []
        plan_lines: List[str] = []
        for idx, p in enumerate(plan[:6], 1):
            st = (p.get("status") or "").lower()
            if header_override.startswith("✅"):
                icon = "✅"
            else:
                icon = "✅" if st == "completed" else ("🔄" if st == "in_progress" else "⬜")
            plan_lines.append(f"{icon} {idx}. {(p.get('step') or '')[:62]}")
        plan_block = ("\n\n🧭 Rencana Tahap Kerja:\n" + "\n".join(plan_lines)) if plan_lines else ""

        act_txt = self._last_thought or (self.orchestrator.live_status if self.orchestrator else "Memproses...")
        step_prefix = f"Langkah {self._last_step_num}: " if self._last_step_num > 0 else ""
        url_line = f"\n🌐 Web: {cur_u}" if cur_u else ""
        return f"{hdr}{url_line}\n📍 {step_prefix}{act_txt[:185]}{plan_block}"

    def start_progress_heartbeat(self) -> None:
        """Detak indikator live setiap 4 detik selama bot bekerja: 'sedang mengetik...' nonstop + stopwatch berjalan!"""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        tick = 0
        try:
            while self.orchestrator and self.orchestrator.state == "RUNNING":
                targets = {self.owner_id} if self.is_locked and self.owner_id else set(self.active_chat_ids)
                for cid in targets:
                    asyncio.create_task(self._send_chat_action(cid, "typing"))
                await asyncio.sleep(4.2)
                if not self.orchestrator or self.orchestrator.state != "RUNNING":
                    break
                tick += 1
                card_txt = self._build_progress_card_text(tick_idx=tick)
                kb = self._running_inline_kb()
                for cid in targets:
                    mid = self._status_msg_ids.get(cid)
                    if mid and self._last_rendered_card.get(cid) != card_txt:
                        self._last_rendered_card[cid] = card_txt
                        await self._edit_text(cid, mid, card_txt, reply_markup=kb)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _finalize_progress_cards(self, header_label: str) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        card_txt = self._build_progress_card_text(header_override=header_label)
        targets = {self.owner_id} if self.is_locked and self.owner_id else set(self.active_chat_ids)
        for cid in targets:
            mid = self._status_msg_ids.get(cid)
            if mid and self._last_rendered_card.get(cid) != card_txt:
                self._last_rendered_card[cid] = card_txt
                await self._edit_text(cid, mid, card_txt, reply_markup={"inline_keyboard": []})

    async def _edit_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.token:
            return
        payload: Dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text[:3900]}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            await get_shared_http().post(
                f"https://api.telegram.org/bot{self.token}/editMessageText",
                json=payload,
                timeout=8.0,
            )
        except Exception:
            pass

    async def _answer_cb(self, cb_id: Optional[str], text: str) -> None:
        if not cb_id or not self.token:
            return
        try:
            await get_shared_http().post(
                f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": text},
                timeout=8.0,
            )
        except Exception:
            pass

    async def send_text(self, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> Optional[int]:
        if not self.token:
            return None
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text[:3900]}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            r = await get_shared_http().post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload,
                timeout=12.0,
            )
            d = r.json()
            if d.get("ok"):
                return d["result"].get("message_id")
        except Exception:
            pass
        return None

    async def notify_step(self, step_num: int, thought: str, plan: Optional[List[Dict[str, Any]]] = None) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        self._last_step_num = step_num
        self._last_thought = (thought or "").strip()
        card_txt = self._build_progress_card_text(tick_idx=step_num)
        kb = self._running_inline_kb()
        targets = {self.owner_id} if self.is_locked and self.owner_id else set(self.active_chat_ids)
        for cid in targets:
            asyncio.create_task(self._send_chat_action(cid, "typing"))
            mid = self._status_msg_ids.get(cid)
            if mid and self._last_rendered_card.get(cid) != card_txt:
                self._last_rendered_card[cid] = card_txt
                await self._edit_text(cid, mid, card_txt, reply_markup=kb)

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
        await get_shared_http().post(f"https://api.telegram.org/bot{self.token}/sendPhoto", data=data, files=files, timeout=18.0)

    async def send_document(self, chat_id: int, filename: str, file_bytes: bytes, caption: str = "") -> None:
        if not self.token:
            return
        data = {"chat_id": str(chat_id), "caption": caption[:950]}
        files = {"document": (filename, file_bytes)}
        await get_shared_http().post(f"https://api.telegram.org/bot{self.token}/sendDocument", data=data, files=files, timeout=22.0)

    async def _delete_msg(self, chat_id: int, message_id: int) -> None:
        if not self.token or not message_id:
            return
        try:
            await get_shared_http().post(
                f"https://api.telegram.org/bot{self.token}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
                timeout=6.0,
            )
        except Exception:
            pass

    async def notify_takeover(self, reason: str, frame_b64: str) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        await self._finalize_progress_cards("⏸️ DIJEDA SEBENTAR — Menunggu Login Akun Lu")
        caption = (
            "🔐 Halaman Ini Butuh Login Bro!\n\n"
            "⚡ CARA PALING GAMPANG (Nggak Usah Buka Layar!):\n"
            "Langsung balas aja di chat ini:\n"
            "username_lu | password_lu\n"
            "(kalau di halaman ada CAPTCHA angka/kode, OTOMATIS dibaca & diisi bot!)\n\n"
            "🧹 Tenang: pesan balasan sandi lu bakal OTOMATIS DIHAPUS dalam 1 detik & nggak pernah masuk ke AI!"
        )
        rows: List[List[Dict[str, Any]]] = []
        live_u = self.protected_web_url()
        if live_u.startswith("https://"):
            rows.append([{"text": "📱 Atau Isi Lewat Form HP Simpel", "url": live_u}])
        rows.append([{"text": "✅ Udah Login — Lanjut Kerjain", "callback_data": "resume_login"}])
        markup = {"inline_keyboard": rows}
        targets = {self.owner_id} if self.is_locked and self.owner_id else set(self.active_chat_ids)
        for cid in targets:
            if frame_b64:
                await self.send_photo_b64(cid, frame_b64, caption, markup)
            else:
                await self.send_text(cid, caption, markup)

    async def notify_approval(self, sens_report: Dict[str, Any], frame_b64: str) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        await self._finalize_progress_cards("⏸️ DIJEDA SEBENTAR — Menunggu Persetujuan Lu")
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
        targets = {self.owner_id} if self.is_locked and self.owner_id else set(self.active_chat_ids)
        for cid in targets:
            if frame_b64:
                await self.send_photo_b64(cid, frame_b64, caption, markup)
            else:
                await self.send_text(cid, caption, markup)

    async def notify_file(self, filename: str, file_bytes: bytes) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        targets = {self.owner_id} if self.is_locked and self.owner_id else set(self.active_chat_ids)
        for cid in targets:
            await self.send_document(cid, filename, file_bytes, f"📄 File selesai dibuat: {filename}")

    async def notify_done(self, summary: str) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        import time as _t
        el_s = max(1, int(_t.monotonic() - self._run_started_at)) if self._run_started_at else 1
        clean = summary.replace("**", "").strip()
        is_failure = clean.startswith("❌") or clean.startswith("⚠️") or clean.startswith("🚫") or "GAGAL Verifikasi" in clean or "BELUM SESUAI" in clean.upper()
        await self._finalize_progress_cards(
            f"⚠️ PEKERJAAN TERHENTI ({el_s} dtk)" if is_failure else f"✅ PEKERJAAN TUNTAS ({el_s} dtk)!"
        )
        export_kb = {
            "inline_keyboard": [
                [
                    {"text": "📘 Word (.docx)", "callback_data": "exp:docx"},
                    {"text": "📗 Excel (.xlsx)", "callback_data": "exp:xlsx"},
                    {"text": "📕 PDF (.pdf)", "callback_data": "exp:pdf"},
                ]
            ]
        }
        out_msg = clean if is_failure else f"✅ Beres!\n\n{clean}"
        targets = {self.owner_id} if self.is_locked and self.owner_id else set(self.active_chat_ids)
        for cid in targets:
            self.last_output_text[cid] = summary
            await self.send_text(cid, out_msg, reply_markup=(MAIN_MENU_KB if is_failure else export_kb))

    async def send_to_all(self, message: str) -> None:
        if not self.is_configured or not self.active_chat_ids:
            return
        targets = {self.owner_id} if self.is_locked and self.owner_id else set(self.active_chat_ids)
        for cid in targets:
            await self.send_text(cid, message, reply_markup=MAIN_MENU_KB)
