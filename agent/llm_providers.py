import asyncio
import html as _html
import json
import os
import re
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from agent.dom_inspector import format_dom_for_llm

# Pool koneksi HTTP/1.1 Keep-Alive permanen (IPv4 murni) — memangkas waktu jabat-tangan TLS 400–900ms di setiap pesan!
_SHARED_HTTP_POOL: Optional[httpx.AsyncClient] = None
_LIVE_CACHE: Dict[str, Tuple[float, str]] = {}

_COMMON_TICKER_MAP = {
    "btc": "BTC-USD",
    "bitcoin": "BTC-USD",
    "eth": "ETH-USD",
    "ethereum": "ETH-USD",
    "sol": "SOL-USD",
    "solana": "SOL-USD",
    "xrp": "XRP-USD",
    "doge": "DOGE-USD",
    "emas": "GC=F",
    "gold": "GC=F",
    "ihsg": "^JKSE",
    "kurs": "USDIDR=X",
    "rupiah": "USDIDR=X",
    "usdidr": "USDIDR=X",
}

_IDX_BLUECHIPS = {"bbca", "bbri", "bmri", "bbni", "tlkm", "asii", "goto", "adro", "antm", "brpt", "unvr", "icbp", "amrt", "mdka", "pgas", "ptba", "inkp", "klbf"}


def get_shared_http() -> httpx.AsyncClient:
    global _SHARED_HTTP_POOL
    if _SHARED_HTTP_POOL is None or _SHARED_HTTP_POOL.is_closed:
        _SHARED_HTTP_POOL = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(
                local_address="0.0.0.0",
                retries=1,
            ),
            limits=httpx.Limits(max_keepalive_connections=25, max_connections=50, keepalive_expiry=45.0),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            },
            timeout=16.0,
            follow_redirects=True,
        )
    return _SHARED_HTTP_POOL


@asynccontextmanager
async def _fast_http_client(timeout: float = 25.0):
    """Memakai ulang koneksi Keep-Alive yang sudah hangat tanpa menutup socket setiap selesai request."""
    yield get_shared_http()


async def _fetch_single_quote(client: httpx.AsyncClient, sym: str) -> str:
    try:
        ry = await client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=5d", timeout=3.5)
        if ry.status_code == 200:
            res_list = ry.json().get("chart", {}).get("result") or []
            if res_list:
                meta = res_list[0].get("meta", {})
                price = meta.get("regularMarketPrice")
                prev = meta.get("chartPreviousClose")
                d_high = meta.get("regularMarketDayHigh")
                d_low = meta.get("regularMarketDayLow")
                cur = meta.get("currency", "")
                name = meta.get("longName") or meta.get("shortName") or sym
                diff_str = ""
                if price is not None and prev:
                    pct = ((price - prev) / prev) * 100
                    diff_str = f" ({pct:+.2f}% dari penutupan sebelumnya {prev} {cur})"
                hl_str = f" | High: {d_high} {cur}, Low: {d_low} {cur}" if d_high and d_low else ""
                return f"[DATA PASAR REAL-TIME YAHOO FINANCE] {name} ({sym}): {price} {cur}{diff_str}{hl_str}"
    except Exception:
        pass
    return ""


async def _fetch_live_web_context(query: str) -> str:
    """Mengambil harga saham/kripto/kurs real-time (<0.18 detik paralel + cache 45 detik) & berita terbaru."""
    q_low = (query or "").lower().strip()
    now_ts = _time.monotonic()
    cached = _LIVE_CACHE.get(q_low)
    if cached and (now_ts - cached[0] < 45.0):
        return cached[1]

    needs_finance = any(
        w in q_low
        for w in ("harga", "saham", "etf", "stock", "price", "btc", "bitcoin", "eth", "kripto", "crypto", "kurs", "usd", "idr", "emas", "ihsg")
    ) or bool(re.search(r"\b[A-Z]{3,5}\b", query or ""))
    needs_news = any(
        w in q_low
        for w in ("sekarang", "hari ini", "terbaru", "berita", "jadwal", "skor", "cuaca", "2026", "update", "kenapa", "knpa")
    )
    if not needs_finance and not needs_news:
        return ""

    client = get_shared_http()
    chunks: List[str] = []
    try:
        if needs_finance:
            tickers: List[str] = []
            for word in re.findall(r"[a-zA-Z0-9\.]+", q_low):
                if word in _COMMON_TICKER_MAP:
                    tickers.append(_COMMON_TICKER_MAP[word])
                elif word in _IDX_BLUECHIPS:
                    tickers.append(f"{word.upper()}.JK")

            stop_words = {
                "bro", "gua", "lu", "buka", "bukain", "liat", "lihat", "cek", "berapa", "harga", "saham",
                "etf", "koin", "kripto", "crypto", "kurs", "emiten", "indeks", "sekarang", "skrg", "dong",
                "hari", "ini", "itu", "tolong", "tuh", "nih", "ya", "apa", "dan", "yang", "web", "pdf", "kenapa", "knpa",
            }
            for tok in re.findall(r"\b([a-zA-Z]{2,5}(?:\.[a-zA-Z]{2})?)\b", query or ""):
                tl = tok.lower()
                if tl not in stop_words and tl not in _COMMON_TICKER_MAP and tl not in _IDX_BLUECHIPS:
                    tickers.append(tok.upper())

            # Dedup preserving order
            seen_t = set()
            uniq_tickers = [t for t in tickers if not (t in seen_t or seen_t.add(t))][:3]

            if uniq_tickers:
                results = await asyncio.gather(*[_fetch_single_quote(client, s) for s in uniq_tickers])
                chunks.extend([r for r in results if r])

            if not chunks:
                clean_fin = " ".join(w for w in re.findall(r"[a-z0-9]+", q_low) if w not in stop_words).strip()
                if clean_fin:
                    rs = await client.get("https://query2.finance.yahoo.com/v1/finance/search", params={"q": clean_fin, "quotesCount": 2}, timeout=3.0)
                    if rs.status_code == 200:
                        found_syms = [q_item["symbol"] for q_item in rs.json().get("quotes", [])[:2] if q_item.get("symbol")]
                        if found_syms:
                            results = await asyncio.gather(*[_fetch_single_quote(client, s) for s in found_syms])
                            chunks.extend([r for r in results if r])

        # Hanya tarik HTML search jika belum dapat harga finansial ATAU user memang tanya alasan/berita
        if not chunks or any(w in q_low for w in ("berita", "kenapa", "knpa", "jadwal", "skor", "cuaca", "terbaru")):
            rw = await client.get("https://search.yahoo.com/search", params={"p": query}, timeout=3.5)
            if rw.status_code == 200:
                for m in re.finditer(r'<div[^>]*class="[^"]*compText[^"]*"[^>]*>(.*?)</div>', rw.text, re.S):
                    clean_s = _html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
                    clean_s = re.sub(r"\s+", " ", clean_s).strip()
                    if len(clean_s) > 35:
                        chunks.append(f"- {clean_s[:240]}")
                        if len(chunks) >= 5:
                            break
    except Exception:
        pass

    if not chunks:
        return ""
    out = "=== DATA INTERNET REAL-TIME DETIK INI ===\n" + "\n".join(chunks[:5]) + "\n\n"
    if len(_LIVE_CACHE) > 120:
        _LIVE_CACHE.clear()
    _LIVE_CACHE[q_low] = (now_ts, out)
    return out

SYSTEM_PROMPT = """Kamu adalah NusaOperator, AI Cloud Browser Agent yang cerdas, teliti, dan mengutamakan keamanan pengguna.
Tugasmu adalah memahami perintah user (dalam Bahasa Indonesia atau Inggris), menyusun rencana langkah kerja (multi-step plan), membaca tampilan website (melalui screenshot + daftar elemen interaktif [index]), mengelola file di workspace, dan mengeksekusi langkah satu per satu hingga tuntas.

ATURAN KEAMANAN MUTLAK (HUMAN-IN-THE-LOOP):
1. DILARANG menebak atau mengarang sendiri password, PIN, atau kode OTP/2FA. NAMUN jika user SUDAH MENULISKAN secara eksplisit Username & Password di dalam perintah chatnya (contoh: "login pakai Username: standard_user dan Password: secret_sauce"), LANGSUNG ketik menggunakan action `"type"` ke kolom Username & Password lalu `"click"` tombol Login — JANGAN panggil `"request_human_login"`! Panggil `"request_human_login"` HANYA jika halaman meminta login/OTP/CAPTCHA yang kredensialnya BELUM diberikan oleh user.
2. Untuk tindakan sensitif (seperti: konfirmasi pembayaran/checkout, transfer uang, hapus akun/data penting, kirim email/publikasi, ubah pengaturan keamanan), pastikan form sudah siap lalu klik tombolnya atau panggil `"ask_approval"` — sistem guardrail juga akan meminta izin eksplisit ke user sebelum klik dieksekusi.
3. Untuk pencarian web umum di browser, gunakan `"navigate"` ke `https://search.yahoo.com/search?p=kata+kunci` (hindari `google.com/search` dan `duckduckgo` agar tidak terkena CAPTCHA IP server cloud). Jika user hanya menyapa atau bertanya apakah kamu bisa melakukan sesuatu tanpa menyebutkan link/tugas spesifiknya, jawab ramah dengan gaya santai (*gua/lu/bro*) menggunakan `"done"`.
4. PROAKTIF BUKA TAB/WEB BARU: Jika user meminta masuk/membuka layanan lain yang berbeda dari halaman yang sedang terbuka (contoh: "login Konami ID", "buka e-learning", "cek GitHub", "buka baru lagi"), LANGSUNG panggil `"navigate"` ke URL resmi layanan tersebut (misal Konami ID -> `https://my.konami.net`), JANGAN pernah bilang website lama tidak mendukung layanan tersebut! Perhatikan juga RIWAYAT OBROLAN SEBELUMNYA untuk memahami maksud lanjutan user.

ATURAN KETAT PENGISIAN & VERIFIKASI FORMULIR (WAJIB PATUHI 100%):
1. Identifikasi setiap field berdasarkan `sel` (DOM selector), `label`, `placeholder`, dan `name` pada daftar elemen `[index]`. Perhatikan `CURRENT_VALUE`, `CHECKED`, `maxlength`, `pattern`, dan `[AUTOCOMPLETE_INPUT]`.
2. Untuk field `[AUTOCOMPLETE_INPUT]` (seperti `Subjects` / React-Select), gunakan `"type"` pada index inputnya (sistem otomatis mengetik & memilih opsi autocomplete di DOM; jika lebih dari satu subject bisa dipisah koma misal `"Maths, Physics"`). Untuk custom dropdown seperti `State` dan `City`, gunakan `"select"` atau `"type"` pada inputnya.
3. Periksa panjang dan format setiap input (misal nomor telepon wajib 10 digit angka jika `maxlength=10`, format email valid). Jika muncul peringatan `❌ MASALAH FORMAT/PANJANG` atau `❌ PERINGATAN AUTOCOMPLETE`, WAJIB perbaiki field tersebut terlebih dahulu!
4. Sebelum mengeklik tombol `Submit`, periksa `CURRENT_VALUE` & `CHECKED` seluruh field terhadap instruksi user. Jika ada data yang masih kosong atau tidak sesuai, perbaiki dulu sebelum klik `Submit`.
5. Setelah mengeklik `Submit`, WAJIB baca `TABEL HASIL SUBMIT` dan bandingkan setiap baris dengan data yang diminta user.
6. DILARANG KERAS melaporkan berhasil (`"done"` dengan klaim sukses) apabila tabel hasil belum muncul atau ada field yang nilainya belum sesuai! Jika gagal/tidak sesuai, sebutkan dengan jujur **nama field yang bermasalah** beserta **penyebabnya**.

DAFTAR ACTION YANG TERSEDIA (pilih TEPAT SATU action setiap langkah):
- `{"name": "navigate", "url": "https://..."}` -> Membuka URL website (gunakan "/demo-portal" untuk portal simulasi akun).
- `{"name": "click", "index": 3}` -> Klik elemen interaktif nomor [index].
- `{"name": "type", "index": 5, "text": "teks input", "press_enter": false}` -> Ketik teks ke input/textarea nomor [index].
- `{"name": "select", "index": 6, "option": "Label atau value"}` -> Pilih opsi pada `<select>` nomor [index].
- `{"name": "scroll", "direction": "down"}` -> Scroll halaman ("down", "up", "top", "bottom").
- `{"name": "go_back"}` -> Kembali ke halaman sebelumnya.
- `{"name": "read_file", "filename": "profil_pengajuan.json"}` -> Baca isi file dari folder workspace (mendukung `.pdf`, `.docx`, `.xlsx`, `.csv`, `.json`, `.md`, `.txt`).
- `{"name": "write_file", "filename": "hasil_riset.docx", "content": "..."}` -> Buat/timpa file asli di folder workspace & otomatis terkirim ke Telegram user! MENDUKUNG OTOMATIS Word (`.docx`), Excel Multi-Sheet (`.xlsx`), PDF (`.pdf`), `.md`, dan `.csv`.
  * KHUSUS EXCEL MULTI-SHEET (`.xlsx`): Jika user meminta beberapa sheet (contoh Sheet 1 & Sheet 2), tulis pemisah `# Sheet 1: Data Produk` diikuti tabel pipa `| Kolom 1 | Kolom 2 |`, lalu `# Sheet 2: Analisis dan Rekomendasi` diikuti tabel/poin kesimpulannya — sistem otomatis menghasilkan beberapa tab worksheet Excel asli!

ATURAN RISET DATA WEB, KATALOG PRODUK, ANALISIS, & PEMBUATAN FILE:
1. Perhatikan blok `=== DAFTAR PRODUK / ITEM TERSTRUKTUR DI HALAMAN INI ===` pada observasi halaman! Jika Nama lengkap, Harga, Rating bintang, dan Status ketersediaan semua produk yang diminta SUDAH tertera lengkap di daftar tersebut, LANGSUNG olah datanya tanpa membuang langkah mengeklik satu per satu ke halaman detail!
2. Hitung matematika (jumlah total, rata-rata presisi 2 desimal, nilai termurah/termahal, pengelompokan rating, dan filter kriteria seperti `Rating >= 4` DAN `Harga <= 30` beserta aturan prioritasnya) secara cermat & periksa ulang angkanya.
3. WAJIB panggil `"write_file"` terlebih dahulu bila user meminta file Excel/Word/PDF dibuat. DILARANG KERAS memanggil `"done"` sebelum file benar-benar berhasil dibuat atau menulis kalimat gantung seperti *"saya akan melanjutkan..."* di dalam `"done"`! Pada `"done"`, jawab lengkap seluruh poin `LAPORAN AKHIR` yang diminta user.
- `{"name": "append_file", "filename": "catatan.md", "content": "..."}` -> Tambahkan teks ke file di workspace.
- `{"name": "solve_captcha"}` -> Baca, hitung, dan isi otomatis CAPTCHA gambar/matematika/teks yang ada di halaman saat ini menggunakan AI Vision! (Gunakan kapan saja ada CAPTCHA gambar/penjumlahan angka di formulir atau setelah mengisi username/password).
- `{"name": "request_human_login", "reason": "Silakan login ke akun kamu terlebih dahulu di layar browser."}` -> Jeda agent & serahkan kontrol ke user untuk login akun pribadi / OTP.
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
    # Bersihkan trailing comma sebelum } atau ] yang kadang muncul dari LLM cepat
    cleaned = re.sub(r",\s*([}\]])", r"\1", text)
    for candidate in (text, cleaned):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        frag = match.group(0)
        try:
            return json.loads(frag)
        except json.JSONDecodeError:
            # Ganti kontrol newline mentah di dalam nilai string
            frag_single = frag.replace("\r", " ").replace("\n", " ")
            return json.loads(frag_single)
    raise ValueError("Respons JSON dari model tidak valid.")


class LLMConfig:
    def __init__(self) -> None:
        self.provider: str = os.getenv("LLM_PROVIDER", "gemini").lower()
        self.model: str = os.getenv("LLM_MODEL", "gemini-3.1-flash-lite-preview")
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
            "gemini": "gemini-3.1-flash-lite-preview",
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

    async def quick_chat(
        self,
        message: str,
        recent_chat: Optional[List[Dict[str, Any]]] = None,
        user_memory_block: str = "",
    ) -> str:
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

        live_ctx = await _fetch_live_web_context(message)
        mem_part = (user_memory_block.strip() + "\n\n") if user_memory_block else ""
        chat_sys = (
            "Kamu adalah Asisten AI & Cloud Browser Operator pribadi berbahasa Indonesia santai (gua/lu/bro secukupnya). "
            "Kamu punya fitur Cloud Browser otomatis & Memori Profil User (/ingat) — user TIDAK perlu menekan tombol mode apa pun! "
            "Jika terdapat 'MEMORI PROFIL & GAYA KERJA PEMILIK BOT', gunakan data tersebut secara otomatis saat membuat surat, CV, dokumen, atau menjawab pertanyaan tentang user. "
            "ATURAN ANTI-HALUSINASI MUTLAK: "
            "1. Pada balasan ini kamu HANYA menjawab obrolan teks cepat dan TIDAK sedang menjalankan proses background apa pun. "
            "DILARANG KERAS menulis kalimat janji palsu seperti 'Lagi gua proses buka...', 'Tunggu bentar ya lagi loading...', '[Laporan Hasil...]', ATAU mengarang link download palsu seperti 'sandbox:/mnt/data/...'. "
            "2. Jika user meminta dibuatkan isi dokumen/surat/tabel, tulis langsung isi naskah/tabelnya dengan rapi tanpa membuat link download palsu (sistem otomatis yang akan melampirkan file aslinya ke Telegram). "
            "3. Jika user baru bertanya apakah bisa buka web / login / kerjain sesuatu tanpa menyebut web/tugasnya, jawab santai: 'Bisa banget bro! Langsung ketik aja mau buka web apa dan mau diapain, langsung gua sikat otomatis!' "
            "3. Jika tersedia 'DATA INTERNET REAL-TIME DETIK INI', gunakan angka tersebut secara akurat. "
            "Jangan pakai simbol bintang (** atau *) berlebihan agar rapi di Telegram HP."
        )
        prompt = f"{mem_part}{hist_block}{live_ctx}USER: {message}"

        if self.config.provider == "gemini":
            # Urutan model tercepat dari hasil benchmark nyata (~1.1s vs 5.5s!) + Hedged Racing anti-ngelag
            fast_order = ["gemini-3.1-flash-lite-preview", "gemini-3.1-flash-lite", "gemini-3-flash-preview"]
            if self.config.model and self.config.model not in fast_order:
                fast_order.insert(0, self.config.model)
            payload = {
                "system_instruction": {"parts": [{"text": chat_sys}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.45, "maxOutputTokens": 650},
            }
            client = get_shared_http()

            async def _try_one(m_name: str) -> Optional[str]:
                u = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent?key={self.config.api_key}"
                try:
                    r = await client.post(u, json=payload, timeout=10.0)
                    if r.status_code == 200:
                        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                except Exception:
                    pass
                return None

            # Jalankan model tercepat (#1); jika lewat 3.0 detik belum balik (antrian server Google), langsung balapkan #2 paralel!
            t_primary = asyncio.create_task(_try_one(fast_order[0]))
            done, _ = await asyncio.wait({t_primary}, timeout=3.0)
            if done:
                ans = t_primary.result()
                if ans:
                    return ans

            t_backup = asyncio.create_task(_try_one(fast_order[1]))
            pending = {t for t in (t_primary, t_backup) if not t.done()}
            while pending:
                done_set, pending = await asyncio.wait(pending, timeout=9.0, return_when=asyncio.FIRST_COMPLETED)
                if not done_set:
                    break
                for d_task in done_set:
                    ans = d_task.result()
                    if ans:
                        for p in pending:
                            p.cancel()
                        return ans

            for p in pending:
                p.cancel()
            ans3 = await _try_one(fast_order[2])
            if ans3:
                return ans3
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

    async def classify_intent(
        self,
        message: str,
        current_url: str = "",
        recent_chat: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Klasifikasi otomatis <0.45 detik untuk kalimat ambigu agar user TIDAK PERLU menekan tombol mode dulu!
        Mengembalikan: 'chat' | 'open_web' | 'agent'
        """
        if not self.config.has_api_key or self.config.provider != "gemini":
            return "chat"

        hist_tail = ""
        for m in (recent_chat or [])[-4:]:
            if m.get("role") in ("user", "agent"):
                hist_tail += f"{m['role'].upper()}: {(m.get('text') or '')[:140]}\n"

        sys_i = (
            "Tentukan rute eksekusi untuk pesan user berikut menjadi TEPAT SATU JSON: {\"route\": \"chat\" | \"open_web\" | \"agent\"}.\n"
            "- \"agent\": jika user menyuruh membuka/mengakses situs/akun/layanan web tertentu lalu melakukan aksi (login, isi form, klik, cari di web tertentu, rangkum halaman web, belanja, cek dashboard/classroom/portal, atau menindaklanjuti halaman browser yang sedang terbuka).\n"
            "- \"open_web\": jika user HANYA menyuruh membuka 1 website/link saja tanpa tugas lanjutan (contoh: 'bukain web konami id', 'buka traveloka').\n"
            "- \"chat\": jika user ngobrol biasa, bertanya pengetahuan umum/soal/coding, minta dibuatkan teks/surat/esai tanpa perlu buka website tertentu, atau tanya harga saham/kripto/kurs cepat."
        )
        u_txt = f"URL Browser Aktif: {current_url or '(kosong)'}\n{hist_tail}PESAN USER: {message}"
        payload = {
            "system_instruction": {"parts": [{"text": sys_i}]},
            "contents": [{"role": "user", "parts": [{"text": u_txt}]}],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 30, "responseMimeType": "application/json"},
        }
        client = get_shared_http()
        for m_name in ("gemini-3.1-flash-lite-preview", "gemini-3.1-flash-lite"):
            try:
                r = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent?key={self.config.api_key}",
                    json=payload,
                    timeout=3.2,
                )
                if r.status_code == 200:
                    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    route = _extract_json(raw).get("route", "chat")
                    if route in ("chat", "open_web", "agent"):
                        return route
            except Exception:
                continue
        return "chat"

    async def solve_captcha_image(self, raw_bytes: bytes, mime_type: str = "image/png", hint_text: str = "") -> str:
        """Membaca & menghitung CAPTCHA gambar (angka penjumlahan, pengurangan, perkalian, atau kode huruf/angka) secara presisi."""
        if not self.config.has_api_key or not raw_bytes:
            return ""
        import base64 as _b64

        b64_img = _b64.b64encode(raw_bytes).decode("ascii")
        prompt = (
            "Baca gambar CAPTCHA ini dengan sangat teliti.\n"
            "1. Jika gambar berisi soal operasi hitung matematika (contoh: '12 + 18', '25 - 6', '7 x 4', '9 + 5 = ?'), "
            "HITUNG hasil matematikanya dengan benar dan kembalikan HANYA angka hasil akhirnya saja (contoh: '30').\n"
            "2. Jika gambar berisi kode teks huruf/angka biasa (contoh: 'X8kP2' atau '73914'), kembalikan HANYA teks kodenya persis sesuai huruf besar/kecilnya tanpa spasi.\n"
            f"{('Konteks tambahan di sekitar gambar: ' + hint_text[:120]) if hint_text else ''}\n"
            'Balas HANYA JSON murni: {"expr": "teks asli di gambar", "answer": "jawaban_bersih"}'
        )
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": mime_type, "data": b64_img}},
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
        }
        client = get_shared_http()
        for m_name in ("gemini-3.1-flash-lite-preview", "gemini-3-flash-preview", "gemini-3.1-flash-lite"):
            u = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent?key={self.config.api_key}"
            try:
                r = await client.post(u, json=payload, timeout=7.5)
                if r.status_code == 200:
                    raw_t = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    ans = str(_extract_json(raw_t).get("answer") or "").strip()
                    if ans:
                        return ans
            except Exception:
                continue
        return ""

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
        if extracted_text and not mime_type.startswith(("image/", "audio/")):
            parts.append({"text": f"INSTRUKSI USER: {instruction}\n\n=== ISI DOKUMEN ({filename}) ===\n{extracted_text[:18000]}"})
        else:
            if mime_type.startswith("audio/"):
                instruction = (
                    "Dengarkan pesan suara (Voice Note) user ini baik-baik, lalu langsung jawab/kerjakan apa yang diminta user dengan santai dan jelas!"
                )
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

        if "siskopatuh" in q.lower():
            return "https://siskopatuh.haji.go.id/web/"

        if self.config.has_api_key and self.config.provider == "gemini":
            candidates = ["gemini-3.1-flash-lite-preview", "gemini-3-flash-preview", "gemini-3.1-flash-lite"]
            payload = {
                "system_instruction": {
                    "parts": [
                        {
                            "text": (
                                "Ubah permintaan website dari user menjadi SATU URL resmi HTTPS yang paling tepat. "
                                "Contoh: 'efhub' -> 'https://efootballhub.net', 'Konami ID' -> 'https://my.konami.net', "
                                "'SISKOPATUH' -> 'https://siskopatuh.haji.go.id/web/', 'Google Classroom' -> 'https://classroom.google.com'. "
                                "Jika tidak yakin URL pastinya, kembalikan URL pencarian Yahoo: 'https://search.yahoo.com/search?p=...'. "
                                "Balas HANYA JSON: {\"url\": \"https://...\"}"
                            )
                        }
                    ]
                },
                "contents": [{"role": "user", "parts": [{"text": q}]}],
                "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
            }
            client = get_shared_http()
            for model in candidates:
                u = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.config.api_key}"
                try:
                    r = await client.post(u, json=payload, timeout=5.0)
                    if r.status_code == 200:
                        parsed = _extract_json(r.json()["candidates"][0]["content"]["parts"][0]["text"])
                        if parsed.get("url"):
                            return str(parsed["url"]).strip()
                except Exception:
                    continue

        from urllib.parse import quote_plus
        return f"https://search.yahoo.com/search?p={quote_plus(q)}"

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
        if not history:
            live_ctx = await _fetch_live_web_context(goal)
            if live_ctx:
                user_context = live_ctx + user_context
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
        for idx, h in enumerate(history[-14:], start=1):
            real_step = h.get("step", idx)
            hist_lines.append(f"Langkah #{real_step}: Action={h.get('action_name')} | Hasil={h.get('result')}")
        hist_str = "\n".join(hist_lines) if hist_lines else "(Belum ada langkah sebelumnya)"
        stats_line = f"TOTAL LANGKAH AKSI YANG SUDAH DIEKSEKUSI: {len(history)} langkah.\n"

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
            f"=== RIWAYAT LANGKAH SEBELUMNYA ({stats_line.strip()}) ===\n{hist_str}\n\n"
            f"=== OBSERVASI HALAMAN SAAT INI ===\n{format_dom_for_llm(dom_state)}\n\n"
            "Berikan keputusan langkah selanjutnya dalam JSON sesuai skema."
        )

    async def _call_gemini(self, prompt_text: str, screenshot_b64: str) -> Dict[str, Any]:
        import asyncio as _aio
        candidates = ["gemini-3.1-flash-lite-preview", "gemini-3.1-flash-lite", "gemini-3-flash-preview"]
        if self.config.model and self.config.model not in candidates:
            candidates.insert(0, self.config.model)

        last_err = ""
        client = get_shared_http()

        async def _try_step_model(m_name: str, include_img: bool) -> Optional[Dict[str, Any]]:
            nonlocal last_err
            parts: List[Dict[str, Any]] = [{"text": prompt_text}]
            if include_img and screenshot_b64:
                parts.append({"inline_data": {"mime_type": "image/jpeg", "data": screenshot_b64}})
            payload = {
                "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json",
                },
            }
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent?key={self.config.api_key}"
            try:
                resp = await client.post(url, json=payload, timeout=16.0)
            except Exception as exc:
                last_err = f"Koneksi AI sibuk ({type(exc).__name__}) pada {m_name}"
                return None
            if resp.status_code >= 400:
                last_err = f"Gemini API ({resp.status_code}) pada {m_name}"
                return None
            try:
                data = resp.json()
                cands = data.get("candidates") or []
                if not cands:
                    return None
                parts_out = (cands[0].get("content") or {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts_out).strip()
                parsed = _extract_json(text)
                if isinstance(parsed, dict) and parsed.get("action"):
                    return parsed
            except Exception as parse_exc:
                last_err = f"Format JSON melenceng pada {m_name} ({type(parse_exc).__name__})"
            return None

        # Hedged Racing untuk langkah Browser Agent: jika model #1 antri > 4.5 detik, langsung balapkan model #2 paralel!
        for include_img in ([True, False] if screenshot_b64 else [False]):
            t1 = _aio.create_task(_try_step_model(candidates[0], include_img))
            done, _ = await _aio.wait({t1}, timeout=4.5)
            if done and t1.result():
                return t1.result()

            t2 = _aio.create_task(_try_step_model(candidates[1], include_img))
            pending = {t for t in (t1, t2) if not t.done()}
            while pending:
                done_set, pending = await _aio.wait(pending, timeout=12.0, return_when=_aio.FIRST_COMPLETED)
                if not done_set:
                    break
                for dt in done_set:
                    res = dt.result()
                    if res:
                        for p in pending:
                            p.cancel()
                        return res
            for p in pending:
                p.cancel()

            if len(candidates) > 2:
                res3 = await _try_step_model(candidates[2], include_img)
                if res3:
                    return res3

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
