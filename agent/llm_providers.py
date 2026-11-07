import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from agent.dom_inspector import format_dom_for_llm


def _fast_http_client(timeout: float = 18.0) -> httpx.AsyncClient:
    """Paksa IPv4 agar di container cloud tanpa Outbound IPv6 tidak pernah delay 10 detik."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
        timeout=timeout,
    )

SYSTEM_PROMPT = """Kamu adalah NusaOperator, AI Cloud Browser Agent yang cerdas, teliti, dan mengutamakan keamanan pengguna.
Tugasmu adalah memahami perintah user (dalam Bahasa Indonesia atau Inggris), menyusun rencana langkah kerja (multi-step plan), membaca tampilan website (melalui screenshot + daftar elemen interaktif [index]), mengelola file di workspace, dan mengeksekusi langkah satu per satu hingga tuntas.

ATURAN KEAMANAN MUTLAK (HUMAN-IN-THE-LOOP):
1. DILARANG KERAS menebak atau mengetik sendiri password, PIN, kode OTP/2FA, atau menyelesaikan CAPTCHA. Jika halaman membutuhkan login/autentikasi dan user belum masuk, WAJIB panggil action `"request_human_login"` agar user login sendiri secara aman di layar browser.
2. Untuk tindakan sensitif (seperti: konfirmasi pembayaran/checkout, transfer uang, hapus akun/data penting, kirim email/publikasi, ubah pengaturan keamanan), pastikan form sudah siap lalu klik tombolnya atau panggil `"ask_approval"` — sistem guardrail juga akan meminta izin eksplisit ke user sebelum klik dieksekusi.
3. Untuk pencarian web umum, gunakan `"navigate"` ke `https://html.duckduckgo.com/html/?q=kata+kunci` (hindari `google.com/search` agar tidak terkena CAPTCHA IP server cloud). Jika user hanya menyapa atau bertanya apakah kamu bisa melakukan sesuatu tanpa menyebutkan link/tugas spesifiknya, jawab ramah dengan gaya santai (*gua/lu/bro*) menggunakan `"done"`.
4. PROAKTIF BUKA TAB/WEB BARU: Jika user meminta masuk/membuka layanan lain yang berbeda dari halaman yang sedang terbuka (contoh: "login Konami ID", "buka e-learning", "cek GitHub", "buka baru lagi"), LANGSUNG panggil `"navigate"` ke URL resmi layanan tersebut (misal Konami ID -> `https://my.konami.net`), JANGAN pernah bilang website lama tidak mendukung layanan tersebut! Perhatikan juga RIWAYAT OBROLAN SEBELUMNYA untuk memahami maksud lanjutan user.

DAFTAR ACTION YANG TERSEDIA (pilih TEPAT SATU action setiap langkah):
- `{"name": "navigate", "url": "https://..."}` -> Membuka URL website (gunakan "/demo-portal" untuk portal simulasi akun).
- `{"name": "click", "index": 3}` -> Klik elemen interaktif nomor [index].
- `{"name": "type", "index": 5, "text": "teks input", "press_enter": false}` -> Ketik teks ke input/textarea nomor [index].
- `{"name": "select", "index": 6, "option": "Label atau value"}` -> Pilih opsi pada `<select>` nomor [index].
- `{"name": "scroll", "direction": "down"}` -> Scroll halaman ("down", "up", "top", "bottom").
- `{"name": "go_back"}` -> Kembali ke halaman sebelumnya.
- `{"name": "read_file", "filename": "profil_pengajuan.json"}` -> Baca isi file dari folder workspace.
- `{"name": "write_file", "filename": "hasil_riset.md", "content": "..."}` -> Buat/timpa file di folder workspace.
- `{"name": "append_file", "filename": "catatan.md", "content": "..."}` -> Tambahkan teks ke file di workspace.
- `{"name": "request_human_login", "reason": "Silakan login ke akun kamu terlebih dahulu di layar browser."}` -> Jeda agent & serahkan kontrol layar ke user untuk login/2FA/CAPTCHA.
- `{"name": "ask_approval", "index": 9, "reason": "Konfirmasi pembayaran sebesar Rp ..."}` -> Minta izin user sebelum mengeksekusi elemen sensitif [index].
- `{"name": "done", "summary": "Ringkasan lengkap hasil pekerjaan untuk user."}` -> Pekerjaan selesai.

FORMAT OUTPUT WAJIB JSON MURNI (tanpa teks di luar JSON):
{
  "thought": "Analisis singkat kondisi layar saat ini dan langkah berikutnya",
  "plan": [
    {"step": "Langkah 1", "status": "completed | in_progress | pending"}
  ],
  "action": {
    "name": "..."
  }
}
"""


def _extract_json(raw_text: str) -> Dict[str, Any]:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return json.loads(match.group(0))
        raise


class LLMConfig:
    def __init__(self) -> None:
        self.provider: str = os.getenv("LLM_PROVIDER", "gemini").lower()
        self.model: str = os.getenv("LLM_MODEL", "gemini-3.1-flash-lite")
        self.api_key: str = (
            os.getenv("LLM_API_KEY")
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY")
            or ""
        ).strip()
        self.base_url: str = os.getenv("LLM_BASE_URL", "").strip()

        if not os.getenv("LLM_PROVIDER"):
            if os.getenv("OPENAI_API_KEY"):
                self.provider = "openai"
                self.model = "gpt-4o"
            elif os.getenv("ANTHROPIC_API_KEY"):
                self.provider = "anthropic"
                self.model = "claude-sonnet-4-20250514"

    def update(self, provider: str, model: str, api_key: str, base_url: str = "") -> None:
        self.provider = (provider or "gemini").strip().lower()
        self.model = (model or "").strip() or self._default_model(self.provider)
        if api_key is not None and api_key != "********":
            self.api_key = api_key.strip()
        self.base_url = (base_url or "").strip()

    @staticmethod
    def _default_model(provider: str) -> str:
        defaults = {
            "gemini": "gemini-3.1-flash-lite",
            "openai": "gpt-4o",
            "anthropic": "claude-sonnet-4-20250514",
            "openrouter": "google/gemini-3.1-flash-lite",
        }
        return defaults.get(provider, "gpt-4o")

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def public_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "has_api_key": self.has_api_key,
            "masked_key": ("••••" + self.api_key[-4:]) if len(self.api_key) > 4 else ("Terpasang" if self.api_key else ""),
            "base_url": self.base_url,
            "mode": f"Vision LLM ({self.provider.upper()}: {self.model})" if self.has_api_key else "Smart Autopilot (Tanpa API Key)",
        }


class MultiLLMBrain:
    """
    Menghubungkan Orchestrator ke provider Vision LLM pilihan user (Gemini, OpenAI, Claude, OpenRouter),
    serta menyediakan Smart Autopilot bawaan agar seluruh fitur dapat langsung diuji tanpa API key.
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self.autopilot = SmartAutopilotPlanner()

    async def quick_chat(self, message: str, recent_chat: Optional[List[Dict[str, Any]]] = None) -> str:
        """Mode Ngobrol AI cepat (<1 detik) tanpa beban browser."""
        if not self.config.has_api_key:
            return (
                "Halo bro! Gua siap ngobrol, tapi pastiin API Key Gemini udah dipasang ya biar gua bisa mikir & jawab pertanyaan lu bebas."
            )

        hist_lines = []
        for m in (recent_chat or [])[-10:]:
            r = m.get("role", "")
            if r in ("user", "agent"):
                lbl = "USER" if r == "user" else "ASISTEN"
                hist_lines.append(f"{lbl}: {(m.get('text') or '')[:400]}")
        hist_block = ("\n".join(hist_lines) + "\n\n") if hist_lines else ""

        chat_sys = (
            "Kamu adalah Asisten AI pribadi yang pinter, santai, dan akrab berbahasa Indonesia sehari-hari (pake gaya santai gua/lu/bro secukupnya). "
            "Bantu user ngobrol, diskusi, ngerjain soal/tugas, merangkum materi, atau nulis teks dengan jelas, rapi, dan cepet. "
            "Jangan pakai format Markdown rumit yang berlebihan agar enak dibaca di layar HP Telegram."
        )
        prompt = f"{hist_block}USER: {message}"

        if self.config.provider == "gemini":
            primary = self.config.model or "gemini-3.1-flash-lite"
            candidates = [primary] + [m for m in ("gemini-3.1-flash-lite", "gemini-3-flash-preview") if m != primary]
            payload = {
                "system_instruction": {"parts": [{"text": chat_sys}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.6},
            }
            async with _fast_http_client(timeout=16.0) as client:
                for model in candidates:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.config.api_key}"
                    try:
                        resp = await client.post(url, json=payload)
                        if resp.status_code == 200:
                            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    except Exception:
                        continue
            return "Waduh koneksi AI lagi agak padat bro, coba kirim ulang barusan ya!"

        # Fallback OpenAI/OpenRouter
        base = self.config.base_url or ("https://openrouter.ai/api/v1" if self.config.provider == "openrouter" else "https://api.openai.com/v1")
        async with httpx.AsyncClient(timeout=16.0) as client:
            resp = await client.post(
                base.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json={
                    "model": self.config.model or "gpt-4o",
                    "messages": [{"role": "system", "content": chat_sys}, {"role": "user", "content": prompt}],
                },
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
        return "Koneksi AI sedang sibuk, coba kirim lagi bro."

    async def analyze_media(
        self,
        user_prompt: str,
        filename: str,
        raw_bytes: bytes,
        mime_type: str,
        extracted_text: str = "",
    ) -> str:
        """Membaca foto (soal, form, KTP/paspor, tabel) atau dokumen (PDF, Word, Excel) dari Telegram."""
        if not self.config.has_api_key:
            if extracted_text:
                return f"📄 Isi teks dari {filename}:\n\n{extracted_text[:2500]}"
            return "Pasang API Key Gemini dulu bro biar gua bisa baca foto & dokumen otomatis."

        import base64 as _b64

        sys_txt = (
            "Kamu adalah Asisten AI pribadi berbahasa Indonesia santai (gua/lu/bro secukupnya). "
            "Tugasmu membaca foto atau dokumen yang dikirim user dengan teliti. "
            "Jika berisi soal/tugas: langsung kerjakan dan berikan jawaban jelas langkah demi langkah. "
            "Jika berisi formulir/data identitas/jadwal/tabel: ekstrak semua data pentingnya dengan rapi agar siap dipakai mengisi web. "
            "Hindari simbol Markdown rumit yang berlebihan supaya rapi dibaca di Telegram HP."
        )

        instruction = (user_prompt or "").strip()
        if not instruction:
            instruction = (
                f"Tolong baca file '{filename}' ini secara lengkap. "
                "Kalau ini soal/tugas, langsung jawab dengan jelas. "
                "Kalau ini dokumen/data/identitas/jadwal, rangkum & ekstrak poin-poin datanya secara rapi."
            )

        parts: List[Dict[str, Any]] = []
        if extracted_text and not mime_type.startswith("image/"):
            parts.append({"text": f"INSTRUKSI USER: {instruction}\n\n=== ISI DOKUMEN ({filename}) ===\n{extracted_text[:18000]}"})
        else:
            parts.append({"text": f"INSTRUKSI USER ({filename}): {instruction}"})
            if raw_bytes and len(raw_bytes) <= 15 * 1024 * 1024:
                b64_data = _b64.b64encode(raw_bytes).decode("ascii")
                parts.append({"inline_data": {"mime_type": mime_type, "data": b64_data}})

        primary = self.config.model or "gemini-3.1-flash-lite"
        candidates = [primary] + [m for m in ("gemini-3.1-flash-lite", "gemini-3-flash-preview") if m != primary]
        payload = {
            "system_instruction": {"parts": [{"text": sys_txt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.25},
        }
        async with _fast_http_client(timeout=24.0) as client:
            for model in candidates:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.config.api_key}"
                try:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                except Exception:
                    continue

        if extracted_text:
            return f"📄 Isi {filename} berhasil dibaca:\n\n{extracted_text[:2000]}"
        return f"Waduh, gagal menganalisis {filename} karena server AI lagi sibuk. Coba kirim ulang ya bro!"

    async def resolve_website_url(self, raw_query: str) -> str:
        """Mengubah nama layanan (misal 'login Konami ID' atau 'elearning UT') menjadi URL resmi."""
        q = (raw_query or "").strip()
        if q.startswith(("http://", "https://", "/demo-portal")):
            return q
        # Cek apakah sudah berbentuk domain (contoh: allindonesia.imigrasi.go.id)
        if "." in q and " " not in q:
            return f"https://{q}"

        if self.config.has_api_key and self.config.provider == "gemini":
            primary = self.config.model or "gemini-3.1-flash-lite"
            candidates = [primary] + [m for m in ("gemini-3.1-flash-lite", "gemini-3-flash-preview") if m != primary]
            payload = {
                "system_instruction": {
                    "parts": [
                        {
                            "text": (
                                "Ubah permintaan website dari user menjadi SATU URL resmi HTTPS yang paling tepat. "
                                "Contoh: 'Konami ID' -> 'https://my.konami.net', 'All Indonesia' -> 'https://allindonesia.imigrasi.go.id', "
                                "'Google Classroom' -> 'https://classroom.google.com'. "
                                "Jika tidak yakin URL pastinya, kembalikan URL pencarian DuckDuckGo HTML: 'https://html.duckduckgo.com/html/?q=...'. "
                                "Balas HANYA JSON: {\"url\": \"https://...\"}"
                            )
                        }
                    ]
                },
                "contents": [{"role": "user", "parts": [{"text": q}]}],
                "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
            }
            async with _fast_http_client(timeout=10.0) as client:
                for model in candidates:
                    u = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.config.api_key}"
                    try:
                        r = await client.post(u, json=payload)
                        if r.status_code == 200:
                            parsed = _extract_json(r.json()["candidates"][0]["content"]["parts"][0]["text"])
                            if parsed.get("url"):
                                return str(parsed["url"]).strip()
                    except Exception:
                        continue

        from urllib.parse import quote_plus
        return f"https://html.duckduckgo.com/html/?q={quote_plus(q)}"

    async def decide_next_step(
        self,
        goal: str,
        dom_state: Dict[str, Any],
        history: List[Dict[str, Any]],
        workspace_files: List[Dict[str, Any]],
        memory_notes: Dict[str, str],
        recent_chat: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if not self.config.has_api_key:
            return self.autopilot.decide(goal, dom_state, history, workspace_files, memory_notes)

        user_context = self._build_user_prompt(goal, dom_state, history, workspace_files, memory_notes, recent_chat)
        screenshot_b64 = dom_state.get("screenshot_b64", "")

        provider = self.config.provider
        if provider == "gemini":
            return await self._call_gemini(user_context, screenshot_b64)
        if provider == "anthropic":
            return await self._call_anthropic(user_context, screenshot_b64)
        return await self._call_openai_compatible(user_context, screenshot_b64)

    def _build_user_prompt(
        self,
        goal: str,
        dom_state: Dict[str, Any],
        history: List[Dict[str, Any]],
        workspace_files: List[Dict[str, Any]],
        memory_notes: Dict[str, str],
        recent_chat: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        files_str = ", ".join(f"{f['name']} ({f['size']}B)" for f in workspace_files) or "(kosong)"
        hist_lines = []
        for idx, h in enumerate(history[-12:], start=1):
            hist_lines.append(f"{idx}. Action={h.get('action_name')} | Hasil={h.get('result')}")
        hist_str = "\n".join(hist_lines) if hist_lines else "(Belum ada langkah sebelumnya)"

        chat_str = ""
        if recent_chat:
            c_lines = []
            for m in recent_chat[-8:]:
                r = m.get("role", "")
                if r in ("user", "agent", "takeover"):
                    lbl = "USER" if r == "user" else "ASISTEN"
                    c_lines.append(f"- {lbl}: {(m.get('text') or '')[:260]}")
            if c_lines:
                chat_str = "=== RIWAYAT OBROLAN SEBELUMNYA ===\n" + "\n".join(c_lines) + "\n\n"

        mem_str = ""
        if memory_notes:
            mem_items = [f"- {k}: {v[:900]}" for k, v in memory_notes.items()]
            mem_str = "\n=== CATATAN / ISI FILE YANG SUDAH DIBACA ===\n" + "\n".join(mem_items) + "\n"

        return (
            f"{chat_str}"
            f"PERINTAH USER SAAT INI: {goal}\n\n"
            f"FILE DI WORKSPACE: {files_str}\n"
            f"{mem_str}\n"
            f"=== RIWAYAT LANGKAH SEBELUMNYA ===\n{hist_str}\n\n"
            f"=== OBSERVASI HALAMAN SAAT INI ===\n{format_dom_for_llm(dom_state)}\n\n"
            "Berikan keputusan langkah selanjutnya dalam JSON sesuai skema."
        )

    async def _call_gemini(self, prompt_text: str, screenshot_b64: str) -> Dict[str, Any]:
        import asyncio as _aio
        primary = self.config.model or "gemini-3.1-flash-lite"
        candidates = [primary] + [m for m in ("gemini-3.1-flash-lite", "gemini-3-flash-preview") if m != primary]
        parts: List[Dict[str, Any]] = [{"text": prompt_text}]
        if screenshot_b64:
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": screenshot_b64}})

        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }
        last_err = ""
        async with _fast_http_client(timeout=18.0) as client:
            for model in candidates:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.config.api_key}"
                try:
                    resp = await client.post(url, json=payload)
                except Exception as exc:
                    last_err = f"Koneksi AI lambat ({type(exc).__name__}) pada {model}"
                    continue
                if resp.status_code in (404, 429, 503):
                    last_err = f"Gemini API ({resp.status_code}) pada {model}"
                    if resp.status_code == 429:
                        await _aio.sleep(1.5)
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(f"Gemini API error ({resp.status_code}): {resp.text[:300]}")
                if model != self.config.model:
                    self.config.model = model
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return _extract_json(text)
        raise RuntimeError(last_err or "Server AI sedang padat sesaat, coba kirim ulang 5 detik lagi.")

    async def _call_openai_compatible(self, prompt_text: str, screenshot_b64: str) -> Dict[str, Any]:
        base = self.config.base_url
        if not base:
            base = "https://openrouter.ai/api/v1" if self.config.provider == "openrouter" else "https://api.openai.com/v1"
        url = base.rstrip("/") + "/chat/completions"

        user_content: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        if screenshot_b64:
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}", "detail": "auto"},
                }
            )

        payload: Dict[str, Any] = {
            "model": self.config.model or "gpt-4o",
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        if "openai.com" in url:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"LLM API error ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            return _extract_json(text)

    async def _call_anthropic(self, prompt_text: str, screenshot_b64: str) -> Dict[str, Any]:
        url = (self.config.base_url or "https://api.anthropic.com/v1").rstrip("/") + "/messages"
        content_blocks: List[Dict[str, Any]] = []
        if screenshot_b64:
            content_blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": screenshot_b64},
                }
            )
        content_blocks.append({"type": "text", "text": prompt_text})

        payload = {
            "model": self.config.model or "claude-sonnet-4-20250514",
            "max_tokens": 1400,
            "temperature": 0.2,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content_blocks}],
        }
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"Anthropic API error ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            return _extract_json(text)


class SmartAutopilotPlanner:
    """
    Planner heuristik multi-tahap cerdas agar aplikasi langsung berfungsi 100% di browser nyata
    bahkan sebelum user memasukkan API Key LLM eksternal.
    Mendukung:
    1. Operasi Akun + Login User (HITL) + Isi Form dari File + Persetujuan Pembayaran/Hapus Akun
    2. Riset Web Multi-Tahap (Wikipedia, Hacker News, Books/Quotes, DuckDuckGo, atau URL bebas) + Simpan File
    3. Pengisian Formulir Otomatis dari Workspace File
    4. Navigasi, Klik, Ketik, Scroll, & Rangkum Halaman secara langsung
    """

    def decide(
        self,
        goal: str,
        dom_state: Dict[str, Any],
        history: List[Dict[str, Any]],
        workspace_files: List[Dict[str, Any]],
        memory_notes: Dict[str, str],
    ) -> Dict[str, Any]:
        g = goal.lower()
        url = dom_state.get("url", "")
        url_disp = dom_state.get("url_display", url)
        elements = dom_state.get("elements", [])
        signals = dom_state.get("signals", {})

        # 1. Alur Akun / Portal / Login / Checkout / Hapus Akun Sensitif
        if any(
            kw in g
            for kw in [
                "akun",
                "portal",
                "login",
                "checkout",
                "bayar",
                "pesanan",
                "tagihan",
                "hapus akun",
                "simulasi",
                "formulir pengajuan",
            ]
        ) and not any(kw in g for kw in ["wikipedia", "hacker news", "ycombinator", "books.toscrape"]):
            return self._flow_portal_account(goal, dom_state, history, workspace_files, memory_notes)

        # 2. Alur Riset Wikipedia + Rangkum ke File
        if "wikipedia" in g or ("cari" in g and "ensiklopedia" in g):
            return self._flow_wikipedia_research(goal, dom_state, history)

        # 3. Alur Riset Berita / Hacker News / Katalog Buku + Simpan File
        if any(kw in g for kw in ["hacker news", "berita", "ycombinator", "artikel", "kumpulkan", "riset", "scrape", "buku", "books"]):
            return self._flow_web_research_to_file(goal, dom_state, history)

        # 4. Jika halaman apapun saat ini punya password field dan belum login
        if signals.get("has_password_field") and not any(
            h.get("action_name") == "request_human_login" for h in history[-2:]
        ):
            return {
                "thought": "Terdeteksi halaman login dengan kolom password. Demi keamanan akun lu, kontrol layar diserahkan ke lu untuk login.",
                "plan": [
                    {"step": "Buka halaman target", "status": "completed"},
                    {"step": "Login manual oleh pemilik akun (Human Takeover)", "status": "in_progress"},
                    {"step": "Lanjutkan instruksi setelah login", "status": "pending"},
                ],
                "action": {
                    "name": "request_human_login",
                    "reason": "Halaman ini membutuhkan login akun. Silakan ketik username & password langsung di layar browser sebelah kanan, lalu klik 'Lanjutkan Agent'.",
                },
            }

        # 5. Perintah umum bebas (Buka URL, Klik, Ketik, Simpan halaman ke file)
        return self._flow_general_command(goal, dom_state, history)

    # -------------------- FLOW 1: PORTAL AKUN + FORM + SENSITIVE APPROVAL --------------------
    def _flow_portal_account(
        self,
        goal: str,
        dom_state: Dict[str, Any],
        history: List[Dict[str, Any]],
        workspace_files: List[Dict[str, Any]],
        memory_notes: Dict[str, str],
    ) -> Dict[str, Any]:
        g = goal.lower()
        url_disp = dom_state.get("url_display", "")
        elements = dom_state.get("elements", [])
        signals = dom_state.get("signals", {})
        readable = dom_state.get("readable_text", "")
        wants_delete = "hapus" in g or "delete" in g

        plan = [
            {"step": "Buka Portal Akun Pengguna", "status": "completed" if "/demo-portal" in url_disp else "in_progress"},
            {"step": "Autentikasi pemilik akun (Login di tangan user)", "status": "pending"},
            {"step": "Baca file profil_pengajuan.json dari workspace", "status": "pending"},
            {
                "step": "Hapus data/akun di menu Keamanan (Butuh Izin User)"
                if wants_delete
                else "Isi formulir layanan & konfirmasi pembayaran (Butuh Izin User)",
                "status": "pending",
            },
            {"step": "Simpan bukti aktivitas ke file workspace", "status": "pending"},
        ]

        if "/demo-portal" not in url_disp:
            return {
                "thought": "Membuka halaman NusaCloud Account Portal terlebih dahulu.",
                "plan": plan,
                "action": {"name": "navigate", "url": "/demo-portal"},
            }

        # Cek apakah masih di layar login
        if signals.get("has_password_field"):
            plan[1]["status"] = "in_progress"
            return {
                "thought": "Halaman meminta autentikasi password. Menyerahkan layar ke user agar kredensial tetap 100% di tangan pemilik akun.",
                "plan": plan,
                "action": {
                    "name": "request_human_login",
                    "reason": "Portal Akun membutuhkan login. Silakan masuk di layar browser (bisa klik tombol 'Isi Akun Demo' di layar portal atau ketik manual), lalu klik tombol hijau 'Selesai Login — Lanjutkan Agent'.",
                },
            }

        plan[1]["status"] = "completed"

        # Cek apakah bukti transaksi / hasil akhir sudah muncul di layar
        if "INV-NUSA-" in readable or "TRANSAKSI BERHASIL" in readable.upper():
            already_saved = any(h.get("action_name") == "write_file" for h in history)
            if not already_saved:
                plan[2]["status"] = "completed"
                plan[3]["status"] = "completed"
                plan[4]["status"] = "in_progress"
                report = (
                    f"# Bukti Eksekusi Pesanan — NusaOperator\n\n"
                    f"- **Waktu Eksekusi**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"- **Status Persetujuan**: Disetujui langsung oleh pemilik akun (Human-in-the-Loop)\n\n"
                    f"## Ringkasan Tampilan Bukti\n\n```\n{readable[:1200]}\n```\n"
                )
                return {
                    "thought": "Pembayaran yang telah disetujui user berhasil diproses! Sekarang menyimpan bukti invoice ke workspace.",
                    "plan": plan,
                    "action": {"name": "write_file", "filename": "bukti_transaksi_akun.md", "content": report},
                }

            for p in plan:
                p["status"] = "completed"
            return {
                "thought": "Seluruh tahapan mulai dari login user, pembacaan file, pengisian form, persetujuan pembayaran, hingga simpan bukti telah selesai.",
                "plan": plan,
                "action": {
                    "name": "done",
                    "summary": "Pekerjaan selesai! Kamu sudah login sendiri dengan aman, data dari `profil_pengajuan.json` telah diisikan ke formulir, transaksi sensitif telah dieksekusi atas persetujuanmu, dan bukti invoice sudah disimpan ke `bukti_transaksi_akun.md` di tab File Workspace.",
                },
            }

        # Jika user menolak aksi sensitif sebelumnya, akhiri dengan sopan
        if history and "DITOLAK oleh user" in (history[-1].get("result") or ""):
            plan[2]["status"] = "completed"
            plan[3]["status"] = "completed"
            return {
                "thought": "User memilih menolak tindakan sensitif. Menghentikan eksekusi sesuai keputusan pemilik akun.",
                "plan": plan,
                "action": {
                    "name": "done",
                    "summary": "Tindakan sensitif dibatalkan sesuai keputusanmu. Formulir sudah terisi namun tidak dieksekusi lebih lanjut — kontrol penuh tetap di tanganmu.",
                },
            }

        # Baca file profil dari workspace jika belum dibaca
        if "profil_pengajuan.json" not in memory_notes and not wants_delete:
            plan[2]["status"] = "in_progress"
            return {
                "thought": "Sudah berhasil masuk ke dashboard akun! Membaca data `profil_pengajuan.json` dari workspace untuk mengisi formulir.",
                "plan": plan,
                "action": {"name": "read_file", "filename": "profil_pengajuan.json"},
            }
        plan[2]["status"] = "completed"
        plan[3]["status"] = "in_progress"

        # Jika user meminta hapus akun/data -> buka tab Keamanan lalu klik tombol Hapus
        if wants_delete:
            del_btn = self._find_el(elements, ["hapus akun", "delete account", "hapus data"])
            if del_btn:
                return {
                    "thought": "Menemukan tombol penghapusan akun yang bersifat sensitif/destruktif. Meminta persetujuan user sebelum klik.",
                    "plan": plan,
                    "action": {"name": "click", "index": del_btn["index"]},
                }
            sec_tab = self._find_el(elements, ["keamanan", "security", "pengaturan"])
            if sec_tab:
                return {
                    "thought": "Membuka tab Pengaturan & Keamanan Akun.",
                    "plan": plan,
                    "action": {"name": "click", "index": sec_tab["index"]},
                }

        # Parse JSON profil
        profile: Dict[str, Any] = {}
        try:
            profile = json.loads(memory_notes.get("profil_pengajuan.json", "{}"))
        except Exception:
            profile = {}

        # Pastikan tab Formulir & Checkout terbuka jika belum terlihat input nama
        name_input = self._find_input(elements, ["nama", "full_name", "penerima"])
        if not name_input:
            order_tab = self._find_el(elements, ["formulir", "pengajuan", "pesanan baru", "buat pesanan", "order"])
            if order_tab:
                return {
                    "thought": "Membuka menu Formulir Pengajuan & Checkout di dalam akun.",
                    "plan": plan,
                    "action": {"name": "click", "index": order_tab["index"]},
                }

        # Isi kolom satu per satu jika masih kosong atau berbeda
        field_mappings = [
            (["nama", "full_name"], profile.get("nama_lengkap", "Rizky Pratama")),
            (["perusahaan", "company", "organisasi"], profile.get("perusahaan", "PT Kreasi Digital Nusantara")),
            (["telepon", "phone", "whatsapp", "hp"], profile.get("telepon", "0812-8899-7744")),
            (["alamat", "address"], profile.get("alamat_pengiriman", "Jl. Jenderal Sudirman Kav. 52, Jakarta")),
            (["catatan", "notes", "keterangan"], profile.get("catatan", "Aktifkan invoice pajak otomatis.")),
        ]

        for keywords, target_val in field_mappings:
            inp = self._find_input(elements, keywords)
            if inp and target_val and inp.get("text", "").strip() != target_val.strip():
                return {
                    "thought": f"Mengisi kolom '{inp.get('label') or inp.get('placeholder') or keywords[0]}' menggunakan data dari file workspace.",
                    "plan": plan,
                    "action": {"name": "type", "index": inp["index"], "text": target_val, "press_enter": False},
                }

        # Cek dropdown paket layanan jika ada
        pkg_select = next((e for e in elements if e["tag"] == "select"), None)
        desired_pkg = profile.get("paket_layanan", "Cloud GPU Pro")
        if pkg_select and not any(h.get("action_name") == "select" for h in history):
            return {
                "thought": f"Memilih paket layanan '{desired_pkg}' pada dropdown formulir.",
                "plan": plan,
                "action": {"name": "select", "index": pkg_select["index"], "option": desired_pkg},
            }

        # Klik tombol Konfirmasi & Bayar (akan memicu Sensitive Action Approval Gate!)
        pay_btn = self._find_el(elements, ["bayar", "konfirmasi", "checkout", "proses pesanan", "submit"])
        if pay_btn:
            return {
                "thought": "Seluruh field formulir sudah terisi rapi. Sekarang menekan tombol Konfirmasi & Bayar (sistem akan meminta persetujuanmu terlebih dahulu).",
                "plan": plan,
                "action": {"name": "click", "index": pay_btn["index"]},
            }

        return {
            "thought": "Menyelesaikan pemeriksaan halaman akun.",
            "plan": plan,
            "action": {"name": "done", "summary": "Interaksi pada halaman portal akun telah selesai dijalankan."},
        }

    # -------------------- FLOW 2: RISET WIKIPEDIA MULTI-TAHAP --------------------
    def _flow_wikipedia_research(
        self,
        goal: str,
        dom_state: Dict[str, Any],
        history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        url = dom_state.get("url", "")
        elements = dom_state.get("elements", [])
        readable = dom_state.get("readable_text", "")

        # Ekstrak topik pencarian dari perintah user
        topic = "Kecerdasan buatan"
        m = re.search(r"(?:tentang|topik|mengenai|cari)\s+([a-zA-Z0-9\s\-]+?)(?:\s+di\s+wikipedia|\s+lalu|\s+dan\s+simpan|\s+kemudian|$)", goal, re.IGNORECASE)
        if m and len(m.group(1).strip()) >= 2:
            topic = m.group(1).strip()

        out_file = self._extract_filename(goal, default="ringkasan_wikipedia.md")

        plan = [
            {"step": "Buka id.wikipedia.org", "status": "completed" if "wikipedia.org" in url else "in_progress"},
            {"step": f"Ketik pencarian '{topic}' & navigasi ke artikel", "status": "pending"},
            {"step": "Baca isi artikel tampilan website", "status": "pending"},
            {"step": f"Simpan rangkuman ke file {out_file}", "status": "pending"},
        ]

        if "wikipedia.org" not in url:
            return {
                "thought": "Membuka situs Wikipedia Bahasa Indonesia.",
                "plan": plan,
                "action": {"name": "navigate", "url": "https://id.wikipedia.org"},
            }

        typed_already = any(h.get("action_name") == "type" for h in history)
        if not typed_already:
            plan[1]["status"] = "in_progress"
            search_inp = self._find_input(elements, ["search", "cari", "telusuri", "wikipedia"])
            if search_inp:
                return {
                    "thought": f"Mengetik topik '{topic}' di kolom pencarian Wikipedia lalu menekan Enter.",
                    "plan": plan,
                    "action": {"name": "type", "index": search_inp["index"], "text": topic, "press_enter": True},
                }

        plan[1]["status"] = "completed"

        # Jika berada di halaman hasil pencarian (Special:Search), klik hasil pertama
        if "Istimewa:Pencarian" in url or "Special:Search" in url:
            first_res = next(
                (
                    e
                    for e in elements
                    if e["tag"] == "a"
                    and "/wiki/" in e.get("href", "")
                    and ":" not in e.get("href", "").split("/wiki/")[-1]
                    and len(e.get("text", "")) > 3
                ),
                None,
            )
            if first_res and not any(h.get("action_name") == "click" for h in history[-2:]):
                return {
                    "thought": f"Membuka artikel hasil pencarian teratas: '{first_res.get('text')}'.",
                    "plan": plan,
                    "action": {"name": "click", "index": first_res["index"]},
                }

        # Scroll sekali agar membaca lebih banyak paragraf sebelum menyimpan
        scrolled = any(h.get("action_name") == "scroll" for h in history)
        if not scrolled:
            plan[2]["status"] = "in_progress"
            return {
                "thought": "Membaca bagian utama artikel Wikipedia dan scroll ke bawah untuk mengumpulkan informasi tambahan.",
                "plan": plan,
                "action": {"name": "scroll", "direction": "down"},
            }

        plan[2]["status"] = "completed"
        saved = any(h.get("action_name") == "write_file" for h in history)
        if not saved:
            plan[3]["status"] = "in_progress"
            title = dom_state.get("title", topic)
            md_content = (
                f"# Hasil Riset Wikipedia: {title}\n\n"
                f"- **Sumber URL**: {url}\n"
                f"- **Waktu Pengumpulan**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"## Poin & Kutipan Penting Halaman\n\n"
                f"{readable[:2400]}\n"
            )
            return {
                "thought": f"Informasi artikel sudah terkumpul. Menyimpan laporan rapi ke file `{out_file}` di workspace.",
                "plan": plan,
                "action": {"name": "write_file", "filename": out_file, "content": md_content},
            }

        for p in plan:
            p["status"] = "completed"
        return {
            "thought": "Riset Wikipedia dan penyimpanan file telah selesai.",
            "plan": plan,
            "action": {
                "name": "done",
                "summary": f"Berhasil mencari topik **{topic}** di Wikipedia, membaca isi halaman, dan menyimpan rangkumannya ke file **`{out_file}`** di Workspace!",
            },
        }

    # -------------------- FLOW 3: RISET WEB (HACKER NEWS / KATALOG BUKU) -> FILE --------------------
    def _flow_web_research_to_file(
        self,
        goal: str,
        dom_state: Dict[str, Any],
        history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        g = goal.lower()
        url = dom_state.get("url", "")
        readable = dom_state.get("readable_text", "")
        elements = dom_state.get("elements", [])

        is_books = "buku" in g or "books" in g
        target_site = "https://books.toscrape.com" if is_books else "https://news.ycombinator.com"
        site_label = "Books to Scrape" if is_books else "Hacker News"
        default_file = "katalog_buku.csv" if (is_books and "csv" in g) else ("katalog_buku.md" if is_books else "berita_teknologi_teratas.md")
        out_file = self._extract_filename(goal, default=default_file)

        plan = [
            {"step": f"Buka website {site_label}", "status": "completed" if target_site.split("//")[1] in url else "in_progress"},
            {"step": "Baca tampilan daftar item & navigasi halaman", "status": "pending"},
            {"step": f"Ekstrak & susun data ke file `{out_file}`", "status": "pending"},
        ]

        if target_site.split("//")[1] not in url:
            return {
                "thought": f"Membuka {target_site} untuk mengumpulkan informasi terbaru.",
                "plan": plan,
                "action": {"name": "navigate", "url": target_site},
            }

        scrolled = any(h.get("action_name") == "scroll" for h in history)
        if not scrolled:
            plan[1]["status"] = "in_progress"
            return {
                "thought": f"Membaca daftar konten di {site_label} dan melakukan scroll untuk memuat item lebih lengkap.",
                "plan": plan,
                "action": {"name": "scroll", "direction": "down"},
            }

        plan[1]["status"] = "completed"
        saved = any(h.get("action_name") == "write_file" for h in history)
        if not saved:
            plan[2]["status"] = "in_progress"
            links = [
                f"- [{e['text']}]({e['href']})"
                for e in elements
                if e["tag"] == "a" and len(e.get("text", "")) > 12 and e.get("href", "").startswith("http")
            ][:12]
            links_block = "\n".join(links) if links else readable[:1800]

            if out_file.endswith(".csv"):
                rows = ["no,judul_atau_item,tautan"]
                for idx, e in enumerate(
                    [el for el in elements if el["tag"] == "a" and len(el.get("text", "")) > 8][:15], start=1
                ):
                    clean_t = e["text"].replace('"', "'")
                    rows.append(f'{idx},"{clean_t}","{e.get("href", "")}"')
                file_body = "\n".join(rows) + "\n"
            else:
                file_body = (
                    f"# Laporan Pengumpulan Informasi — {site_label}\n\n"
                    f"- **Sumber**: {url}\n"
                    f"- **Diambil pada**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"## Daftar Temuan Utama\n\n{links_block}\n\n"
                    f"## Cuplikan Tampilan Halaman\n\n{readable[:1500]}\n"
                )

            return {
                "thought": f"Data berhasil diekstrak dari tampilan website. Menyimpan hasil ke `{out_file}`.",
                "plan": plan,
                "action": {"name": "write_file", "filename": out_file, "content": file_body},
            }

        for p in plan:
            p["status"] = "completed"
        return {
            "thought": "Pengumpulan informasi dan pembuatan file laporan telah tuntas.",
            "plan": plan,
            "action": {
                "name": "done",
                "summary": f"Selesai mengumpulkan data dari **{site_label}**! Hasilnya sudah tersimpan rapi di file **`{out_file}`** pada tab File Workspace.",
            },
        }

    # -------------------- FLOW 4: PERINTAH UMUM BEBAS --------------------
    def _flow_general_command(
        self,
        goal: str,
        dom_state: Dict[str, Any],
        history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        g = goal.lower()
        url = dom_state.get("url", "")
        elements = dom_state.get("elements", [])
        readable = dom_state.get("readable_text", "")

        # Cek apakah ada URL eksplisit di perintah user yang belum dibuka
        url_match = re.search(r"(https?://[^\s,]+|(?:[a-z0-9\-]+\.)+(?:com|org|id|net|io|co\.id|dev|ai)(?:/[^\s,]*)?)", goal, re.IGNORECASE)
        if url_match and not any(h.get("action_name") == "navigate" for h in history):
            target_u = url_match.group(1)
            return {
                "thought": f"Membuka website `{target_u}` sesuai perintah user.",
                "plan": [
                    {"step": f"Buka website {target_u}", "status": "in_progress"},
                    {"step": "Baca & tindak lanjuti isi halaman", "status": "pending"},
                ],
                "action": {"name": "navigate", "url": target_u},
            }

        # Jika user minta klik sesuatu
        click_match = re.search(r"klik\s+(?:tombol\s+|link\s+|menu\s+)?['\"]?([^'\"]+)['\"]?", goal, re.IGNORECASE)
        if click_match and not any(h.get("action_name") == "click" for h in history):
            kw = click_match.group(1).strip()
            target_el = self._find_el(elements, [kw])
            if target_el:
                return {
                    "thought": f"Menemukan elemen yang cocok dengan '{kw}' pada indeks [{target_el['index']}]. Melakukan klik.",
                    "plan": [{"step": f"Klik '{kw}'", "status": "in_progress"}],
                    "action": {"name": "click", "index": target_el["index"]},
                }

        # Jika user minta simpan / rangkum halaman ke file
        if any(w in g for w in ["simpan", "rangkum", "tulis", "file", ".md", ".txt", ".csv"]) and not any(
            h.get("action_name") == "write_file" for h in history
        ):
            fname = self._extract_filename(goal, default="rangkuman_halaman.md")
            content = (
                f"# Rangkuman Halaman: {dom_state.get('title', url)}\n\n"
                f"- **URL**: {url}\n"
                f"- **Waktu**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"## Isi Utama\n\n{readable[:2500]}\n"
            )
            return {
                "thought": f"Menyimpan hasil pembacaan tampilan website saat ini ke file `{fname}`.",
                "plan": [
                    {"step": "Baca tampilan halaman aktif", "status": "completed"},
                    {"step": f"Simpan ke file {fname}", "status": "in_progress"},
                ],
                "action": {"name": "write_file", "filename": fname, "content": content},
            }

        # Jika user bertanya / minta mencari sesuatu tanpa URL spesifik -> cari via DuckDuckGo HTML
        if not any(h.get("action_name") == "navigate" for h in history) and any(
            w in g for w in ["cari", "search", "temukan", "info"]
        ):
            q = re.sub(r"^(tolong\s+|coba\s+)?(cari|search|temukan|cari\s+info\s+tentang)\s+", "", goal, flags=re.IGNORECASE).strip()
            from urllib.parse import quote_plus
            return {
                "thought": f"Mencari informasi '{q}' melalui mesin pencari web.",
                "plan": [
                    {"step": f"Cari '{q}' di web", "status": "in_progress"},
                    {"step": "Baca dan rangkum hasil temuan", "status": "pending"},
                ],
                "action": {"name": "navigate", "url": f"https://html.duckduckgo.com/html/?q={quote_plus(q)}"},
            }

        # Default selesai dengan merangkum tampilan halaman yang sedang terbuka
        snippet = readable[:650] if readable else "(Tidak ada teks utama yang terdeteksi)"
        return {
            "thought": "Telah membaca tampilan halaman saat ini dan menyiapkan ringkasan untuk user.",
            "plan": [{"step": "Analisis tampilan halaman & selesaikan instruksi", "status": "completed"}],
            "action": {
                "name": "done",
                "summary": (
                    f"Halaman aktif saat ini: **{dom_state.get('title') or url}** (`{dom_state.get('url_display', url)}`).\n\n"
                    f"**Ringkasan Tampilan:**\n{snippet}\n\n"
                    f"*(Tip: Pasang API Key Gemini / OpenAI / Claude di menu ⚙️ kanan atas jika ingin AI Vision bebas bernalar di website mana pun tanpa batas!)*"
                ),
            },
        }

    # -------------------- HELPERS --------------------
    @staticmethod
    def _find_el(elements: List[Dict[str, Any]], keywords: List[str]) -> Optional[Dict[str, Any]]:
        for kw in keywords:
            kw_l = kw.lower()
            for el in elements:
                hay = " ".join(
                    [
                        el.get("text", ""),
                        el.get("label", ""),
                        el.get("aria_label", ""),
                        el.get("placeholder", ""),
                        el.get("el_id", ""),
                    ]
                ).lower()
                if kw_l in hay:
                    return el
        return None

    @staticmethod
    def _find_input(elements: List[Dict[str, Any]], keywords: List[str]) -> Optional[Dict[str, Any]]:
        inputs = [
            e
            for e in elements
            if e["tag"] in ("input", "textarea")
            and e.get("type") not in ("submit", "button", "checkbox", "radio")
            and not e.get("is_password")
        ]
        for kw in keywords:
            kw_l = kw.lower()
            for el in inputs:
                hay = " ".join(
                    [
                        el.get("label", ""),
                        el.get("placeholder", ""),
                        el.get("name", ""),
                        el.get("el_id", ""),
                        el.get("aria_label", ""),
                    ]
                ).lower()
                if kw_l in hay:
                    return el
        return inputs[0] if (inputs and "search" in keywords) else None

    @staticmethod
    def _extract_filename(goal: str, default: str) -> str:
        m = re.search(r"\b([a-zA-Z0-9_\-]+\.(?:md|txt|csv|json))\b", goal, re.IGNORECASE)
        return m.group(1) if m else default
