import asyncio
import html as _html
import json
import os
import random
import re
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

import httpx

from agent.dom_inspector import format_dom_for_llm
from agent.security import redact_secrets

# Pool koneksi HTTP/1.1 Keep-Alive permanen (IPv4 murni) — memangkas waktu jabat-tangan TLS 400–900ms di setiap pesan!
_SHARED_HTTP_POOL: Optional[httpx.AsyncClient] = None
_LIVE_CACHE: Dict[str, Tuple[float, str]] = {}


class LLMCallError(RuntimeError):
    """Representasi terstruktur kegagalan koneksi/kuota/autentikasi AI setelah evaluasi retry."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        status_code: int = 0,
        retry_after: float = 0.0,
        model_name: str = "",
        exhausted_retry: bool = True,
    ) -> None:
        super().__init__(redact_secrets(message))
        self.kind = kind
        self.status_code = status_code
        self.retry_after = retry_after
        self.model_name = model_name
        self.exhausted_retry = exhausted_retry


def parse_retry_after_seconds(resp: httpx.Response, max_cap: float = 8.0) -> float:
    hdr = (resp.headers.get("retry-after") or resp.headers.get("Retry-After") or "").strip()
    if hdr:
        try:
            val = float(hdr)
            if val > 0:
                return min(max_cap, max(0.4, val))
        except ValueError:
            pass
    try:
        raw_txt = resp.text[:1500]
        m = re.search(r'"retryDelay"\s*:\s*"([0-9]+(?:\.[0-9]+)?)s"', raw_txt)
        if not m:
            m = re.search(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", raw_txt, re.I)
        if m:
            return min(max_cap, max(0.4, float(m.group(1))))
    except Exception:
        pass
    return 0.0


def compute_backoff_delay(
    attempt: int,
    retry_after: float = 0.0,
    base: float = 0.75,
    cap: float = 6.0,
) -> float:
    exp = min(cap, base * (2 ** max(0, attempt)))
    jitter = random.uniform(0.05, 0.30)
    if retry_after > 0:
        return min(cap, max(retry_after, exp * 0.6) + random.uniform(0.02, 0.20))
    return min(cap, exp + jitter)


def classify_http_failure(resp: httpx.Response) -> Tuple[str, str]:
    code = resp.status_code
    body_snip = ""
    try:
        body_snip = redact_secrets((resp.text or "").strip()[:260])
    except Exception:
        body_snip = ""
    low_b = body_snip.lower()
    if code == 429 or (code == 403 and any(k in low_b for k in ("quota", "rate limit", "rate_limit", "resource_exhausted"))):
        return "rate_limit", f"Batas kuota/rate-limit AI tercapai (HTTP {code})."
    if code in (401, 403):
        return "auth_error", f"Autentikasi/izin API Key AI ditolak (HTTP {code})."
    if code == 404:
        return "model_not_found", f"Model AI tidak ditemukan/deprecated (HTTP 404)."
    if code == 400:
        return "bad_request", f"Parameter request AI ditolak (HTTP 400): {body_snip[:140]}"
    if code in (408, 500, 502, 503, 504, 529):
        return "server_overloaded", f"Server AI sedang sibuk/gangguan sementara (HTTP {code})."
    return "http_error", f"HTTP {code}: {body_snip[:140]}"


def reset_shared_http() -> None:
    global _SHARED_HTTP_POOL
    old = _SHARED_HTTP_POOL
    _SHARED_HTTP_POOL = None
    if old is not None and not old.is_closed:
        try:
            asyncio.create_task(old.aclose())
        except Exception:
            pass

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
- `{"name": "make_dir", "path": "PROJECT_ADMIN/DOKUMEN"}` -> Buat folder/subdirektori sungguhan di dalam workspace (bisa juga beberapa sekaligus dipisah koma, contoh `"PROJECT_ADMIN/DOKUMEN, PROJECT_ADMIN/LAPORAN, PROJECT_ADMIN/ARSIP"`).
- `{"name": "list_tree", "path": "PROJECT_ADMIN"}` -> Periksa & tampilkan pohon struktur direktori dan file sungguhan yang benar-benar ada di disk (gunakan untuk memverifikasi seluruh folder & file sebelum memanggil `"done"`!).
- `{"name": "read_file", "filename": "profil_pengajuan.json"}` -> Baca isi file dari folder workspace (mendukung subfolder seperti `"PROJECT_ADMIN/README.txt"` serta format `.pdf`, `.docx`, `.xlsx`, `.csv`, `.json`, `.md`, `.txt`).
- `{"name": "write_file", "filename": "hasil_riset.docx", "content": "..."}` -> Buat/timpa file asli di folder workspace (mendukung path bersarang di dalam subfolder seperti `"PROJECT_ADMIN/DOKUMEN/catatan.txt"`!) & otomatis terkirim ke Telegram user! MENDUKUNG OTOMATIS Word (`.docx`), Excel Multi-Sheet (`.xlsx`), PDF (`.pdf`), `.txt`, `.md`, dan `.csv`.
  * KHUSUS EXCEL MULTI-SHEET (`.xlsx`): Jika user meminta beberapa sheet (contoh Sheet 1 & Sheet 2), tulis pemisah `# Sheet 1: Data Produk` diikuti tabel pipa `| Kolom 1 | Kolom 2 |`, lalu `# Sheet 2: Analisis dan Rekomendasi` diikuti tabel/poin kesimpulannya — sistem otomatis menghasilkan beberapa tab worksheet Excel asli!
  * KHUSUS TUGAS SISTEM FOLDER / DIREKTORI: Kamu MEMILIKI AKSES PENUH ke file system workspace melalui `"make_dir"`, `"write_file"` (dengan path subfolder), `"list_tree"`, maupun `"run_python"` (`os.makedirs`, `open`, `os.walk`). Buat seluruh folder & file sungguhan di disk, verifikasi dengan `"list_tree"`, lalu tampilkan pohon direktori terverifikasi pada `"done"` (sistem juga otomatis membungkus folder menjadi `.zip` untuk dikirim ke Telegram user!). JANGAN PERNAH mengaku tidak memiliki akses filesystem!

ATURAN RISET DATA WEB, KATALOG PRODUK, ANALISIS, & PEMBUATAN FILE:
1. Perhatikan blok `=== DAFTAR PRODUK / ITEM TERSTRUKTUR DI HALAMAN INI ===` pada observasi halaman! Jika Nama lengkap, Harga, Rating bintang, dan Status ketersediaan semua produk yang diminta SUDAH tertera lengkap di daftar tersebut, LANGSUNG olah datanya tanpa membuang langkah mengeklik satu per satu ke halaman detail!
2. Hitung matematika (jumlah total, rata-rata presisi 2 desimal, nilai termurah/termahal, pengelompokan rating, dan filter kriteria seperti `Rating >= 4` DAN `Harga <= 30` beserta aturan prioritasnya) secara cermat & periksa ulang angkanya.
3. WAJIB panggil `"write_file"` terlebih dahulu bila user meminta file Excel/Word/PDF dibuat. DILARANG KERAS memanggil `"done"` sebelum file benar-benar berhasil dibuat atau menulis kalimat gantung seperti *"saya akan melanjutkan..."* di dalam `"done"`! Pada `"done"`, jawab lengkap seluruh poin `LAPORAN AKHIR` yang diminta user.
- `{"name": "append_file", "filename": "catatan.md", "content": "..."}` -> Tambahkan teks ke file di workspace.
- `{"name": "delete_file", "filename": "catatan_lama.txt"}` -> Hapus file di workspace (sistem otomatis meminta persetujuan pengguna terlebih dahulu sebelum menghapus).
- `{"name": "run_js", "code": "return Array.from(document.querySelectorAll('...')).map(e => e.innerText)"}` -> Jalankan JavaScript langsung di halaman browser aktif! Gunakan secara proaktif kapan pun ada data tersembunyi di atribut HTML/CSS/Shadow DOM, tabel besar puluhan baris yang perlu diekstrak cepat 100% akurat sekaligus, atau tombol/elemen custom yang sulit diklik biasa.
- `{"name": "run_python", "code": "import openpyxl ..."}` -> Jalankan kode Python 3 langsung di folder workspace (`openpyxl`, `docx`, `reportlab`, `csv`, `json`, `statistics`, `math` siap pakai)! Gunakan untuk perhitungan angka/statistik rumit agar 0% salah hitung, mengolah data besar, atau membuat file Excel multi-sheet berformula / Word / PDF kustom (setiap file yang disimpan di folder kerja otomatis terkirim ke Telegram user!).
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
        raw_env_model = os.getenv("LLM_MODEL", "").strip()
        if not raw_env_model or "flash-lite" in raw_env_model:
            self.model: str = "gemini-2.5-flash"
        else:
            self.model = raw_env_model
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
        from agent.security import register_runtime_secret

        self.provider = (provider or "gemini").strip().lower()
        self.model = (model or "").strip() or self._default_model(self.provider)
        if api_key is not None and api_key != "********":
            self.api_key = api_key.strip()
            register_runtime_secret(self.api_key)
        self.base_url = (base_url or "").strip()

    @staticmethod
    def _default_model(provider: str) -> str:
        defaults = {
            "gemini": "gemini-2.5-flash",
            "openai": "gpt-4o",
            "anthropic": "claude-sonnet-4-20250514",
            "openrouter": "google/gemini-2.5-flash",
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
        self._rr_idx: int = 0
        self.last_chat_ok: bool = True
        self.last_error_kind: str = ""
        self.last_error_detail: str = ""
        self._unavailable_models: Dict[str, float] = {}
        self._llm_semaphore = asyncio.Semaphore(2)
        self._sleep_fn: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep

    def _is_model_temporarily_unavailable(self, model_name: str) -> bool:
        exp = self._unavailable_models.get(model_name)
        if not exp:
            return False
        if _time.monotonic() >= exp:
            self._unavailable_models.pop(model_name, None)
            return False
        return True

    def _mark_model_unavailable(self, model_name: str, ttl_sec: float = 600.0) -> None:
        self._unavailable_models[model_name] = _time.monotonic() + ttl_sec

    def _gemini_candidate_models(self, *, light_first: bool = False) -> List[str]:
        if light_first:
            base_list = [
                "gemini-3.5-flash-lite",
                "gemini-3.1-flash-lite",
                "gemini-2.5-flash-lite",
                "gemini-2.5-flash",
            ]
        else:
            smart_pool = ["gemini-3.6-flash", "gemini-3-flash-preview", "gemini-2.5-flash"]
            rot = self._rr_idx % len(smart_pool)
            self._rr_idx += 1
            base_list = (
                smart_pool[rot:]
                + smart_pool[:rot]
                + ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]
            )
            cfg_m = (self.config.model or "").strip()
            if cfg_m and "flash-lite" not in cfg_m and cfg_m not in base_list:
                base_list.insert(0, cfg_m)

        avail = [m for m in base_list if not self._is_model_temporarily_unavailable(m)]
        return avail or base_list

    @staticmethod
    def _extract_gemini_text(data: Dict[str, Any]) -> str:
        cands = data.get("candidates") or []
        if not cands:
            return ""
        content = cands[0].get("content") or {}
        parts_out = content.get("parts") or []
        return "".join(str(p.get("text", "")) for p in parts_out if isinstance(p, dict) and not p.get("thought")).strip()

    @staticmethod
    def _friendly_chat_error_message(kind: str) -> str:
        if kind == "auth_error":
            return "❌ API Key AI tidak valid atau belum aktif bro. Cek lagi lewat perintah `/apikey` atau menu pengaturan ya!"
        if kind == "rate_limit":
            return "⏳ Kuota request AI per menit lagi penuh sesaat (HTTP 429). Tunggu sekitar 5–10 detik lalu kirim ulang ya bro!"
        if kind == "timeout":
            return "⏱️ Respons server AI lewat batas waktu (timeout). Coba kirim ulang sebentar lagi ya bro!"
        if kind == "network_error":
            return "🌐 Koneksi jaringan ke server AI sempat terputus. Coba kirim ulang barusan ya bro!"
        return "⚠️ Server AI sedang sibuk sementara (HTTP 503). Coba kirim ulang beberapa detik lagi ya bro!"

    async def _post_gemini_single(
        self,
        model_name: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[LLMCallError]]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.config.api_key}"
        client = get_shared_http()
        try:
            resp = await client.post(url, json=payload, timeout=timeout)
        except httpx.TimeoutException:
            return None, LLMCallError(
                f"Timeout menunggu respons {model_name}",
                kind="timeout",
                model_name=model_name,
            )
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError) as net_exc:
            reset_shared_http()
            return None, LLMCallError(
                f"Koneksi socket AI terputus ({type(net_exc).__name__}) pada {model_name}",
                kind="network_error",
                model_name=model_name,
            )
        except httpx.TransportError as tr_exc:
            return None, LLMCallError(
                f"Gangguan jaringan AI ({type(tr_exc).__name__}) pada {model_name}",
                kind="network_error",
                model_name=model_name,
            )
        except Exception as exc:
            return None, LLMCallError(
                f"Koneksi AI gagal ({type(exc).__name__}) pada {model_name}",
                kind="network_error",
                model_name=model_name,
            )

        if resp.status_code == 200:
            try:
                return resp.json(), None
            except Exception as j_exc:
                return None, LLMCallError(
                    f"Respons JSON HTTP 200 tidak valid dari {model_name} ({type(j_exc).__name__})",
                    kind="invalid_output",
                    status_code=200,
                    model_name=model_name,
                )

        kind, msg = classify_http_failure(resp)
        # Jika model menolak parameter thinkingConfig (HTTP 400), ulang 1x langsung tanpa thinkingConfig!
        gen_cfg = payload.get("generationConfig") or {}
        if resp.status_code == 400 and "thinkingConfig" in gen_cfg:
            clean_payload = dict(payload)
            clean_gen = dict(gen_cfg)
            clean_gen.pop("thinkingConfig", None)
            clean_payload["generationConfig"] = clean_gen
            try:
                resp2 = await get_shared_http().post(url, json=clean_payload, timeout=timeout)
                if resp2.status_code == 200:
                    return resp2.json(), None
                kind, msg = classify_http_failure(resp2)
                resp = resp2
            except Exception:
                pass

        if kind == "model_not_found":
            self._mark_model_unavailable(model_name)

        retry_after = parse_retry_after_seconds(resp) if kind in ("rate_limit", "server_overloaded") else 0.0
        return None, LLMCallError(
            f"{msg} [{model_name}]",
            kind=kind,
            status_code=resp.status_code,
            retry_after=retry_after,
            model_name=model_name,
        )

    async def quick_chat(
        self,
        message: str,
        recent_chat: Optional[List[Dict[str, Any]]] = None,
        user_memory_block: str = "",
    ) -> str:
        """Mode Ngobrol AI cepat dengan Sequential Fallback + Exponential Backoff (tanpa duplikasi request)."""
        if not self.config.has_api_key:
            self.last_chat_ok = False
            self.last_error_kind = "no_api_key"
            self.last_error_detail = "API Key belum terpasang."
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
            "4. Jika tersedia 'DATA INTERNET REAL-TIME DETIK INI', gunakan angka tersebut secara akurat. "
            "Jangan pakai simbol bintang (** atau *) berlebihan agar rapi di Telegram HP."
        )
        prompt = f"{mem_part}{hist_block}{live_ctx}USER: {message}"

        async with self._llm_semaphore:
            if self.config.provider == "gemini":
                fast_order = self._gemini_candidate_models(light_first=False)
                backoff_step = 0
                last_err: Optional[LLMCallError] = None

                for m_name in fast_order:
                    gen_cfg: Dict[str, Any] = {"temperature": 0.4, "maxOutputTokens": 1200}
                    payload = {
                        "system_instruction": {"parts": [{"text": chat_sys}]},
                        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                        "generationConfig": gen_cfg,
                    }
                    data, err = await self._post_gemini_single(m_name, payload, timeout=12.0)
                    if data is not None:
                        ans = self._extract_gemini_text(data)
                        if ans:
                            self.last_chat_ok = True
                            self.last_error_kind = ""
                            self.last_error_detail = ""
                            return ans
                        err = LLMCallError(
                            f"Respons teks kosong dari {m_name}",
                            kind="invalid_output",
                            model_name=m_name,
                        )

                    last_err = err
                    if err and err.kind == "auth_error":
                        break
                    if err and err.kind in ("model_not_found", "bad_request", "invalid_output"):
                        continue
                    if err and err.kind in ("rate_limit", "server_overloaded", "timeout", "network_error"):
                        delay = compute_backoff_delay(backoff_step, retry_after=err.retry_after, base=0.65, cap=4.5)
                        backoff_step += 1
                        await self._sleep_fn(delay)

                err_kind = last_err.kind if last_err else "server_overloaded"
                self.last_chat_ok = False
                self.last_error_kind = err_kind
                self.last_error_detail = str(last_err) if last_err else "Seluruh kandidat model AI gagal merespons."
                return self._friendly_chat_error_message(err_kind)

            # Fallback OpenAI / OpenRouter / Anthropic pada quick_chat dengan retry & backoff
            last_non_gemini_kind = "server_overloaded"
            for attempt in range(3):
                try:
                    if self.config.provider == "anthropic":
                        url = (self.config.base_url or "https://api.anthropic.com/v1").rstrip("/") + "/messages"
                        resp = await get_shared_http().post(
                            url,
                            headers={
                                "x-api-key": self.config.api_key,
                                "anthropic-version": "2023-06-01",
                                "content-type": "application/json",
                            },
                            json={
                                "model": self.config.model or "claude-sonnet-4-20250514",
                                "max_tokens": 1200,
                                "system": chat_sys,
                                "messages": [{"role": "user", "content": prompt}],
                            },
                            timeout=18.0,
                        )
                        if resp.status_code == 200:
                            ans_a = "".join(
                                b.get("text", "") for b in (resp.json().get("content") or []) if b.get("type") == "text"
                            ).strip()
                            if ans_a:
                                self.last_chat_ok = True
                                self.last_error_kind = ""
                                self.last_error_detail = ""
                                return ans_a
                    else:
                        base = self.config.base_url or (
                            "https://openrouter.ai/api/v1" if self.config.provider == "openrouter" else "https://api.openai.com/v1"
                        )
                        resp = await get_shared_http().post(
                            base.rstrip("/") + "/chat/completions",
                            headers={"Authorization": f"Bearer {self.config.api_key}"},
                            json={
                                "model": self.config.model or "gpt-4o",
                                "messages": [{"role": "system", "content": chat_sys}, {"role": "user", "content": prompt}],
                            },
                            timeout=18.0,
                        )
                        if resp.status_code == 200:
                            choices = resp.json().get("choices") or []
                            ans_o = ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
                            if ans_o:
                                self.last_chat_ok = True
                                self.last_error_kind = ""
                                self.last_error_detail = ""
                                return ans_o

                    kind, detail = classify_http_failure(resp)
                    last_non_gemini_kind = kind
                    self.last_error_detail = detail
                    if kind in ("auth_error", "model_not_found", "bad_request"):
                        break
                    if attempt < 2:
                        ra = parse_retry_after_seconds(resp)
                        await self._sleep_fn(compute_backoff_delay(attempt, retry_after=ra, base=0.8, cap=5.0))
                except httpx.TimeoutException:
                    last_non_gemini_kind = "timeout"
                    self.last_error_detail = "Timeout menghubungi provider AI."
                    if attempt < 2:
                        await self._sleep_fn(compute_backoff_delay(attempt, base=0.8, cap=5.0))
                except httpx.TransportError as exc:
                    reset_shared_http()
                    last_non_gemini_kind = "network_error"
                    self.last_error_detail = f"Gangguan jaringan AI ({type(exc).__name__})."
                    if attempt < 2:
                        await self._sleep_fn(compute_backoff_delay(attempt, base=0.8, cap=5.0))
                except Exception as exc:
                    last_non_gemini_kind = "server_overloaded"
                    self.last_error_detail = f"Gangguan respons AI ({type(exc).__name__})."
                    break

            self.last_chat_ok = False
            self.last_error_kind = last_non_gemini_kind
            return self._friendly_chat_error_message(last_non_gemini_kind)

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
            "- \"agent\": jika user menyuruh membuka/mengakses situs web lalu melakukan aksi (login, isi form, klik, scraping, rangkum web), ATAU menyuruh membuat sistem folder/direktori/banyak file sungguhan di file system, menjalankan kode/analisis data Python, atau mengerjakan instruksi multi-langkah kompleks yang wajib diverifikasi.\n"
            "- \"open_web\": jika user HANYA menyuruh membuka 1 website/link saja tanpa tugas lanjutan (contoh: 'bukain web konami id', 'buka traveloka').\n"
            "- \"chat\": jika user ngobrol santai, bertanya pengetahuan umum/soal singkat, minta dibuatkan 1 teks/surat sederhana tanpa struktur folder/browser, atau tanya harga saham/kripto cepat."
        )
        u_txt = f"URL Browser Aktif: {current_url or '(kosong)'}\n{hist_tail}PESAN USER: {message}"
        payload = {
            "system_instruction": {"parts": [{"text": sys_i}]},
            "contents": [{"role": "user", "parts": [{"text": u_txt}]}],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 40, "responseMimeType": "application/json"},
        }
        for m_name in self._gemini_candidate_models(light_first=True)[:3]:
            data, err = await self._post_gemini_single(m_name, payload, timeout=3.5)
            if data is not None:
                try:
                    raw = self._extract_gemini_text(data)
                    route = _extract_json(raw).get("route", "chat")
                    if route in ("chat", "open_web", "agent"):
                        return route
                except Exception:
                    continue
            if err and err.kind == "auth_error":
                break
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
        for idx_m, m_name in enumerate(self._gemini_candidate_models(light_first=True)[:3]):
            data, err = await self._post_gemini_single(m_name, payload, timeout=8.0)
            if data is not None:
                try:
                    raw_t = self._extract_gemini_text(data)
                    ans = str(_extract_json(raw_t).get("answer") or "").strip()
                    if ans:
                        return ans
                except Exception:
                    continue
            if err and err.kind == "auth_error":
                break
            if err and err.kind in ("rate_limit", "server_overloaded") and idx_m < 2:
                await self._sleep_fn(compute_backoff_delay(idx_m, retry_after=err.retry_after, base=0.5, cap=3.0))
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
            self.last_chat_ok = bool(extracted_text)
            self.last_error_kind = "" if extracted_text else "no_api_key"
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

        payload = {
            "system_instruction": {"parts": [{"text": sys_txt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.25},
        }
        async with self._llm_semaphore:
            last_err: Optional[LLMCallError] = None
            backoff_idx = 0
            for model in self._gemini_candidate_models(light_first=False)[:4]:
                data, err = await self._post_gemini_single(model, payload, timeout=24.0)
                if data is not None:
                    txt_out = self._extract_gemini_text(data)
                    if txt_out:
                        self.last_chat_ok = True
                        self.last_error_kind = ""
                        self.last_error_detail = ""
                        return txt_out
                last_err = err
                if err and err.kind == "auth_error":
                    break
                if err and err.kind in ("rate_limit", "server_overloaded", "timeout", "network_error"):
                    await self._sleep_fn(compute_backoff_delay(backoff_idx, retry_after=err.retry_after, base=0.75, cap=4.5))
                    backoff_idx += 1

        if extracted_text:
            self.last_chat_ok = True
            self.last_error_kind = ""
            return f"📄 Isi {filename} berhasil dibaca:\n\n{extracted_text[:2000]}"
        self.last_chat_ok = False
        self.last_error_kind = last_err.kind if last_err else "server_overloaded"
        return f"Waduh, gagal menganalisis {filename} ({self._friendly_chat_error_message(self.last_error_kind)})"

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
            for model in self._gemini_candidate_models(light_first=True)[:3]:
                data, err = await self._post_gemini_single(model, payload, timeout=5.0)
                if data is not None:
                    try:
                        parsed = _extract_json(self._extract_gemini_text(data))
                        if parsed.get("url"):
                            return str(parsed["url"]).strip()
                    except Exception:
                        continue
                if err and err.kind == "auth_error":
                    break

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
        candidates = self._gemini_candidate_models(light_first=False)
        last_err: Optional[LLMCallError] = None
        backoff_step = 0

        async with self._llm_semaphore:
            # Uji dengan screenshot terlebih dahulu; jika gagal karena payload gambar pada model tertentu,
            # hanya coba tanpa gambar 1x pada kandidat sehat bila error bukan auth_error / rate_limit global.
            for m_name in candidates:
                parts: List[Dict[str, Any]] = [{"text": prompt_text}]
                if screenshot_b64:
                    parts.append({"inline_data": {"mime_type": "image/jpeg", "data": screenshot_b64}})
                gen_cfg: Dict[str, Any] = {
                    "temperature": 0.15,
                    "responseMimeType": "application/json",
                }
                payload = {
                    "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                    "contents": [{"role": "user", "parts": parts}],
                    "generationConfig": gen_cfg,
                }
                data, err = await self._post_gemini_single(m_name, payload, timeout=18.0)
                if data is not None:
                    try:
                        text = self._extract_gemini_text(data)
                        parsed = _extract_json(text)
                        act_obj = parsed.get("action") if isinstance(parsed, dict) else None
                        if isinstance(act_obj, dict) and str(act_obj.get("name") or "").strip():
                            return parsed
                        err = LLMCallError(
                            f"Skema JSON action kosong/tidak valid dari {m_name}",
                            kind="invalid_output",
                            model_name=m_name,
                        )
                    except Exception as parse_exc:
                        err = LLMCallError(
                            f"Format JSON melenceng pada {m_name} ({type(parse_exc).__name__})",
                            kind="invalid_output",
                            model_name=m_name,
                        )

                last_err = err
                if err and err.kind == "auth_error":
                    raise err
                if err and err.kind == "bad_request" and screenshot_b64:
                    # Fallback 1x tanpa screenshot pada model yang sama bila gambar ditolak
                    txt_only_payload = {
                        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
                        "generationConfig": gen_cfg,
                    }
                    data2, err2 = await self._post_gemini_single(m_name, txt_only_payload, timeout=15.0)
                    if data2 is not None:
                        try:
                            parsed2 = _extract_json(self._extract_gemini_text(data2))
                            act2 = parsed2.get("action") if isinstance(parsed2, dict) else None
                            if isinstance(act2, dict) and str(act2.get("name") or "").strip():
                                return parsed2
                        except Exception:
                            pass
                    last_err = err2 or err
                    continue
                if err and err.kind in ("model_not_found", "bad_request", "invalid_output"):
                    continue
                if err and err.kind in ("rate_limit", "server_overloaded", "timeout", "network_error"):
                    delay = compute_backoff_delay(backoff_step, retry_after=err.retry_after, base=0.75, cap=5.0)
                    backoff_step += 1
                    await self._sleep_fn(delay)

        if last_err is not None:
            last_err.exhausted_retry = True
            raise last_err
        raise LLMCallError(
            "Server AI sedang sibuk sementara, coba lagi beberapa detik lagi.",
            kind="server_overloaded",
            exhausted_retry=True,
        )

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
        last_err: Optional[LLMCallError] = None
        async with self._llm_semaphore:
            for attempt in range(3):
                try:
                    resp = await get_shared_http().post(url, headers=headers, json=payload, timeout=35.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        text = data["choices"][0]["message"]["content"]
                        parsed = _extract_json(text)
                        act = parsed.get("action") if isinstance(parsed, dict) else None
                        if isinstance(act, dict) and str(act.get("name") or "").strip():
                            return parsed
                        raise ValueError("JSON action kosong")
                    kind, detail = classify_http_failure(resp)
                    ra = parse_retry_after_seconds(resp)
                    last_err = LLMCallError(detail, kind=kind, status_code=resp.status_code, retry_after=ra)
                    if kind in ("auth_error", "model_not_found", "bad_request"):
                        break
                except httpx.TimeoutException:
                    last_err = LLMCallError("Timeout menunggu respons OpenAI/OpenRouter.", kind="timeout")
                except httpx.TransportError as tr_exc:
                    reset_shared_http()
                    last_err = LLMCallError(f"Gangguan jaringan AI ({type(tr_exc).__name__}).", kind="network_error")
                except Exception as exc:
                    last_err = LLMCallError(f"Format respons AI tidak valid ({type(exc).__name__}).", kind="invalid_output")
                    break
                if attempt < 2 and last_err:
                    await self._sleep_fn(compute_backoff_delay(attempt, retry_after=last_err.retry_after, base=0.85, cap=5.0))
        if last_err:
            last_err.exhausted_retry = True
            raise last_err
        raise LLMCallError("Gagal menghubungi provider OpenAI/OpenRouter.", kind="server_overloaded", exhausted_retry=True)

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
        last_err: Optional[LLMCallError] = None
        async with self._llm_semaphore:
            for attempt in range(3):
                try:
                    resp = await get_shared_http().post(url, headers=headers, json=payload, timeout=35.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                        parsed = _extract_json(text)
                        act = parsed.get("action") if isinstance(parsed, dict) else None
                        if isinstance(act, dict) and str(act.get("name") or "").strip():
                            return parsed
                        raise ValueError("JSON action kosong")
                    kind, detail = classify_http_failure(resp)
                    ra = parse_retry_after_seconds(resp)
                    last_err = LLMCallError(detail, kind=kind, status_code=resp.status_code, retry_after=ra)
                    if kind in ("auth_error", "model_not_found", "bad_request"):
                        break
                except httpx.TimeoutException:
                    last_err = LLMCallError("Timeout menunggu respons Anthropic.", kind="timeout")
                except httpx.TransportError as tr_exc:
                    reset_shared_http()
                    last_err = LLMCallError(f"Gangguan jaringan AI ({type(tr_exc).__name__}).", kind="network_error")
                except Exception as exc:
                    last_err = LLMCallError(f"Format respons AI tidak valid ({type(exc).__name__}).", kind="invalid_output")
                    break
                if attempt < 2 and last_err:
                    await self._sleep_fn(compute_backoff_delay(attempt, retry_after=last_err.retry_after, base=0.85, cap=5.0))
        if last_err:
            last_err.exhausted_retry = True
            raise last_err
        raise LLMCallError("Gagal menghubungi provider Anthropic.", kind="server_overloaded", exhausted_retry=True)


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
