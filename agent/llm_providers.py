import asyncio
import html as _html
import json
import os
import random
import re
import time as _time
import urllib.parse
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


# =========================================================================
# MESIN RISET WEB BERLAPIS — supaya AI NYARI DATA DULU sebelum jawab,
# bukan ngarang dari ingatan (penyebab utama jawaban terasa "dangkal").
# =========================================================================
_WEB_CACHE: Dict[str, Tuple[float, str]] = {}
_RESEARCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

# Kata yang menandakan user butuh data SEGAR / faktual (bukan sekadar opini)
_FRESH_INTENT_WORDS = (
    "hari ini", "sekarang", "terbaru", "terkini", "update", "berita", "kabar", "harga",
    "kurs", "skor", "hasil", "jadwal", "rilis", "versi", "tahun", "2025", "2026", "2027",
    "siapa", "kapan", "berapa", "dimana", "di mana", "apa itu", "kenapa", "mengapa",
    "bagaimana", "cara ", "bedanya", "perbedaan", "arti", "definisi", "menurut",
    "data", "statistik", "inflasi", "pdb", "suku bunga", "bi rate", "pemilu", "presiden",
    "saham", "bitcoin", "kripto", "emas", "ihsg", "crypto", "etf", "reksadana",
    "cuaca", "gempa", "banjir", "kecelakaan", "virus", "obat", "resep",
)


async def _research_web(query: str, max_items: int = 6, timeout: float = 9.0, brain_ref: Any = None) -> str:
    """
    Riset web berlapis dengan fallback berantai. Semua sumber dijalankan paralel
    dan yang menjawab duluan dipakai. Kalau semuanya gagal, kembalikan "" (jujur kosong)
    supaya AI tidak mengarang data.
    """
    q = (query or "").strip()
    if not q:
        return ""

    cache_key = "research:" + q.lower()[:120]
    now = _time.monotonic()
    hit = _WEB_CACHE.get(cache_key)
    if hit and (now - hit[0] < 240.0):  # cache 4 menit
        return hit[1]

    results: List[str] = []

    # SUMBER UTAMA: buka mesin pencari di BROWSER SUNGGUHAN (Yahoo).
    # Ini yang bikin pencarian "kelihatan" jalan di Chrome, dan Yahoo terbukti
    # 200 OK dari IP server sementara Google/DDG/Ecosia/Brave semuanya memblokir.
    try:
        browser_rows = await asyncio.wait_for(
            search_via_browser(q, browser=getattr(brain_ref, "browser", None), limit=max_items),
            timeout=32.0,
        )
    except Exception:
        browser_rows = []
    for _t, _u, _sn in browser_rows:
        label = _sn or _t
        results.append(f"{label} — {_u}" if _u else label)

    async def _ddg() -> List[str]:
        """Sumber hasil utama: BING dulu (tahan blokir IP server), DuckDuckGo cadangan."""
        headers = {"User-Agent": _RESEARCH_UA, "Accept-Language": "id-ID,id;q=0.9,en;q=0.8"}
        pairs: List[Tuple[str, str]] = []

        # Bing
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as c:
                rb = await c.get("https://www.bing.com/search", params={"q": q, "setlang": "id"})
            if rb.status_code == 200:
                pairs = [(t, u) for t, u, _sn in _extract_bing_results(rb.text, limit=max_items) if u]
        except Exception:
            pairs = []

        # DuckDuckGo kalau Bing kering
        if not pairs:
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as c:
                    r = await c.get("https://html.duckduckgo.com/html/", params={"q": q})
                if r.status_code == 200:
                    pairs = _extract_result_links(r.text, limit=max_items)
            except Exception:
                pairs = []

        out = [f"{title} — {href}" for title, href in pairs]
        try:
            _LAST_LINKS.clear()
            _LAST_LINKS.extend([h for _t, h in pairs])
        except Exception:
            pass
        return out

    async def _wikipedia() -> List[str]:
        out: List[str] = []
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": _RESEARCH_UA}) as c:
            r = await c.get(
                "https://id.wikipedia.org/w/api.php",
                params={"action": "query", "list": "search", "srsearch": q, "format": "json", "srlimit": 4},
            )
            if r.status_code != 200:
                return []
            for item in (r.json().get("query", {}).get("search") or [])[:4]:
                snip = _html.unescape(re.sub(r"<[^>]+>", " ", item.get("snippet") or ""))
                snip = re.sub(r"\s+", " ", snip).strip()
                if item.get("title"):
                    wurl = f"https://id.wikipedia.org/wiki/{str(item['title']).replace(' ', '_')}"
                    out.append(f"Wikipedia — {item['title']}: {snip[:260]} — {wurl}")
        return out

    async def _google_news() -> List[str]:
        out: List[str] = []
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": _RESEARCH_UA}) as c:
            r = await c.get(
                "https://news.google.com/rss/search",
                params={"q": q, "hl": "id", "gl": "ID", "ceid": "ID:id"},
            )
            if r.status_code != 200:
                return []
            for block in re.findall(r"<item>(.*?)</item>", r.text, re.S)[:max_items]:
                mt = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
                md = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
                if mt:
                    judul = _html.unescape(mt.group(1)).strip()
                    tgl = (md.group(1)[:16] if md else "").strip()
                    out.append(f"Berita {'(' + tgl + ')' if tgl else ''}: {judul[:220]}")
        return out

    async def _fallback_extra() -> List[str]:
        """Cadangan terakhir kalau tiga sumber utama kering.

        Dulu ini DDG Lite, tapi DDG (html & lite) sama-sama membalas HTTP 202
        "anomaly detected" ke IP datacenter, jadi hasilnya SELALU kosong.
        Sekarang pakai Bing + Google News RSS yang jauh lebih toleran.
        """
        out: List[str] = []
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": _RESEARCH_UA}) as c:
                rb = await c.get("https://www.bing.com/search", params={"q": q, "setlang": "id"})
            if rb.status_code == 200:
                for title, href, snip in _extract_bing_results(rb.text, limit=max_items):
                    if snip:
                        out.append(f"{title} — {snip[:200]}" + (f" — {href}" if href else ""))
        except Exception:
            pass
        if not out:
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": _RESEARCH_UA}) as c:
                    r = await c.get(
                        "https://news.google.com/rss/search",
                        params={"q": q, "hl": "id", "gl": "ID", "ceid": "ID:id"},
                    )
                if r.status_code == 200:
                    for block in re.findall(r"<item>(.*?)</item>", r.text, re.S)[:max_items]:
                        mt = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
                        if mt:
                            out.append("Berita: " + _html.unescape(mt.group(1)).strip()[:200])
            except Exception:
                pass
        return out

    if len(results) >= 3:
        # Browser sudah memberi hasil nyata -> cukup. Tambahan HTTP scrape tidak perlu.
        tasks = [asyncio.create_task(_wikipedia()), asyncio.create_task(_google_news())]
    else:
        tasks = [
            asyncio.create_task(_ddg()),
            asyncio.create_task(_wikipedia()),
            asyncio.create_task(_google_news()),
        ]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    for g in gathered:
        if isinstance(g, list) and g:
            results.extend(g)

    # Fallback terakhir kalau tiga sumber utama kering
    if not results:
        try:
            results.extend(await _fallback_extra())
        except Exception:
            pass

    if not results:
        return ""

    # Dedup & batasi
    seen = set()
    uniq: List[str] = []
    for r_item in results:
        key = r_item[:70].lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r_item)
        if len(uniq) >= max_items:
            break

    opsi = (
        "=== HASIL RISET WEB (DATA EKSTERNAL, BUKAN DARI INGATANMU) ===\n"
        + "\n".join(f"- {x}" for x in uniq)
        + "\n=== AKHIR HASIL RISET ===\n"
    )
    if len(_WEB_CACHE) > 80:
        _WEB_CACHE.clear()
    _WEB_CACHE[cache_key] = (now, opsi)
    return opsi


# =========================================================================
# PEMBACA HALAMAN PENUH (#2) — bukan cuma judul/snippet, tapi isi artikelnya.
# =========================================================================
_PAGE_CACHE: Dict[str, Tuple[float, str]] = {}
_LAST_LINKS: List[str] = []
_UNREADABLE_HOSTS = (
    "youtube.com", "youtu.be", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "linkedin.com", "whatsapp.com", "t.me", "pinterest.com",
)


def _html_to_article_text(html: str) -> str:
    """Ekstrak teks artikel bersih dari HTML: buang nav/iklan/sidebar/script, ambil blok utama."""
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception:
        # Fallback tanpa bs4: regex kasar
        body = re.sub(r"(?is)<(script|style|nav|footer|header|aside|form|noscript)[^>]*>.*?</\1>", " ", html)
        body = re.sub(r"(?s)<[^>]+>", " ", body)
        return re.sub(r"\s+", " ", _html.unescape(body)).strip()

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript", "iframe", "svg", "button"]):
        tag.decompose()
    for sel in (".nav", ".navbar", ".menu", ".sidebar", ".ads", ".advertisement", ".cookie", ".popup", ".related"):
        for el in soup.select(sel):
            el.decompose()

    # Cari kontainer artikel paling padat teksnya
    best_txt = ""
    for sel in ("article", "main", "[role=main]", ".post-content", ".entry-content", ".article-body", ".content", "div"):
        for node in soup.select(sel):
            txt = node.get_text(" ", strip=True)
            if len(txt) > len(best_txt):
                best_txt = txt
        if len(best_txt) > 600:
            break

    if not best_txt:
        best_txt = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", best_txt).strip()


def _decode_bing_redirect(href: str) -> str:
    """Bing membungkus URL hasil jadi https://www.bing.com/ck/a?...&u=a1<base64>. Ambil URL aslinya."""
    if "bing.com/ck/a" not in href:
        return href
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        u = (qs.get("u") or [""])[0]
        if u.startswith("a1"):
            b64 = u[2:]
            b64 += "=" * (-len(b64) % 4)
            import base64 as _b64
            return _b64.urlsafe_b64decode(b64).decode("utf-8", "ignore")
    except Exception:
        pass
    return href


def _extract_bing_results(html_txt: str, limit: int = 5) -> List[Tuple[str, str, str]]:
    """Ambil (judul, url, snippet) dari halaman hasil Bing. Bing jauh lebih tahan blokir
    dibanding DuckDuckGo yang sekarang membalas HTTP 202 'anomaly' ke IP server."""
    out: List[Tuple[str, str, str]] = []
    blocks = re.findall(r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', html_txt, re.S)
    for blk in blocks:
        tm = re.search(r"<h2[^>]*>(.*?)</h2>", blk, re.S)
        if not tm:
            continue
        title = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", tm.group(1)))).strip()
        if not title:
            continue
        hm = re.search(r'<a[^>]+href="(https?://[^"]+)"', blk, re.S)
        url = _html.unescape(hm.group(1)) if hm else ""
        url = _decode_bing_redirect(url)
        if not url.startswith("http"):
            url = ""
        sm = re.search(r"<p[^>]*>(.*?)</p>", blk, re.S) or re.search(
            r'class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</div>', blk, re.S
        )
        snip = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", sm.group(1)))).strip() if sm else ""
        out.append((title[:150], url, snip[:300]))
        if len(out) >= limit:
            break
    return out


def _extract_result_links(html: str, limit: int = 5) -> List[Tuple[str, str]]:
    """Ambil pasangan (judul, URL) dari halaman hasil DuckDuckGo."""
    out: List[Tuple[str, str]] = []
    # DDG membungkus URL: //duckduckgo.com/l/?uddg=<encoded>&rut=...
    for m in re.finditer(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        re.S,
    ):
        raw_href, raw_title = m.group(1), m.group(2)
        href = _html.unescape(raw_href)
        if "uddg=" in href:
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                href = qs.get("uddg", [href])[0]
            except Exception:
                pass
            # kadang masih ter-encode sekali lagi
            if href.startswith("//duckduckgo.com/l/"):
                continue
        if not href.startswith("http"):
            continue
        title = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", raw_title))).strip()
        if title and len(title) > 8:
            out.append((title[:150], href))
        if len(out) >= limit:
            break
    return out


async def _fetch_page_text(url: str, max_chars: int = 4000, timeout: float = 10.0) -> str:
    """Buka satu URL dan kembalikan isi artikelnya sebagai teks bersih."""
    u = (url or "").strip()
    if not u.startswith("http"):
        return ""
    host = urllib.parse.urlparse(u).netloc.lower()
    if any(bad in host for bad in _UNREADABLE_HOSTS):
        return ""

    now = _time.monotonic()
    hit = _PAGE_CACHE.get(u)
    if hit and (now - hit[0] < 600.0):  # cache 10 menit
        return hit[1]

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _RESEARCH_UA, "Accept-Language": "id-ID,id,q=0.9,en;q=0.8"},
        ) as c:
            r = await c.get(u)
        if r.status_code != 200:
            return ""
        ctype = (r.headers.get("content-type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return ""
        txt = _html_to_article_text(r.text)
        if len(txt) < 200:
            return ""
        txt = txt[:max_chars]
        if len(_PAGE_CACHE) > 60:
            _PAGE_CACHE.clear()
        _PAGE_CACHE[u] = (now, txt)
        return txt
    except Exception:
        return ""


# =========================================================================
# PENCARIAN LEWAT CHROME SUNGGUHAN (bukan HTTP scrape di belakang layar)
# =========================================================================
# PRINSIP: bot membuka Chromium/Chrome NYATA, lalu MENGETIK di kolom pencarian
# seperti manusia (bukan langsung membuka URL hasil). Ini kuncinya:
#
#   Buka URL hasil langsung     -> Google: "lalu lintas tidak wajar" (BLOKIR)
#   Ketik di kolom + Enter      -> Google: 200 OK, 7 hasil ✅
#
# Hasil uji lapangan dari IP server ini (Chromium nyata):
#   Google      ✅ jalan kalau diketik seperti manusia
#   Bing        ✅ jalan, 10 hasil
#   Yahoo       ✅ jalan, 7 hasil (URL asli bersih)
#   DuckDuckGo  ❌ 202 "anomaly", 0 hasil
#   Ecosia      ❌ 403 CAPTCHA
#   Brave       ❌ 429 CAPTCHA
#   Startpage   ❌ JS challenge tanpa henti
SEARCH_ENGINES: Dict[str, Dict[str, str]] = {
    # nama: {home, input, result_selector, title_selector}
    "google": {
        "label": "Google",
        "home": "https://www.google.com/?hl=id",
        "input": "textarea[name=q], input[name=q]",
        "block_marker": "lalu lintas yang tidak wajar|unusual traffic|recaptcha|verify you are human",
        "result": "div#search a h3, div#rso a h3, a h3",
    },
    "bing": {
        "label": "Bing",
        "home": "https://www.bing.com/?setlang=id",
        "input": "textarea#sb_form_q, input#sb_form_q",
        "block_marker": "recaptcha|verify you are human|sedang memverifikasi",
        "result": "li.b_algo h2",
    },
    "yahoo": {
        "label": "Yahoo",
        "home": "https://search.yahoo.com/",
        "input": "input#yschsp, input[name=p]",
        "block_marker": "recaptcha|are you a robot",
        "result": "div.algo h3",
    },
}

# Urutan percobaan. Bisa diubah user lewat /search google|bing|yahoo
SEARCH_ORDER: List[str] = ["google", "bing", "yahoo"]
DEFAULT_ENGINE = os.getenv("SEARCH_ENGINE_DEFAULT", "google").strip().lower()
if DEFAULT_ENGINE in SEARCH_ENGINES:
    SEARCH_ORDER = [DEFAULT_ENGINE] + [e for e in SEARCH_ORDER if e != DEFAULT_ENGINE]

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

LAST_VISIBLE_SEARCH: Dict[str, Any] = {
    "query": "", "url": "", "engine": "", "count": 0, "tried": [],
}


def set_search_engine(name: str) -> bool:
    """Ubah mesin pencari utama (dipakai oleh /search di Telegram)."""
    global SEARCH_ORDER
    n = (name or "").strip().lower()
    if n not in SEARCH_ENGINES:
        return False
    SEARCH_ORDER = [n] + [e for e in SEARCH_ORDER if e != n]
    return True


def search_order_summary() -> str:
    return " → ".join(SEARCH_ENGINES[e]["label"] for e in SEARCH_ORDER)


async def _resolve_real_url(page: Any, href: str) -> str:
    """Ubah link hasil pencarian jadi URL ASLI situs tujuan.

    - Google: href relatif "/goto?url=CAES..." -> ikuti redirect (302) -> URL asli
    - Bing:   href "https://www.bing.com/ck/a?..." -> decode param u=a1<base64>
    """
    if not href:
        return ""
    # Google /goto -> resolve lewat redirect
    if href.startswith("/goto") or "/goto?url=" in href:
        full = href if href.startswith("http") else ("https://www.google.com" + href)
        try:
            r = await page.context.request.get(full, max_redirects=0, timeout=10000)
            loc = r.headers.get("location") or ""
            if loc.startswith("http"):
                return loc
        except Exception:
            pass
        return ""
    # Bing ck/a -> decode base64
    if "bing.com/ck/a" in href:
        return _decode_bing_redirect(href)
    return href if href.startswith("http") else ""


async def _extract_results_from_page(page: Any, engine: str, limit: int = 5) -> List[Tuple[str, str, str]]:
    """Ambil hasil dari halaman hasil pencarian yang SEDANG TERBUKA di Chrome.

    Dibuat berbasis locator (bukan JS innerHTML) karena tiap mesin beda struktur:
      Google -> judul <h3> di dalam <a>, href relatif /goto?url=... (harus di-resolve)
      Bing   -> judul di <h2>, href dibungkus ck/a (harus di-decode), domain di <cite>
      Yahoo  -> kartu div.algo, href sudah URL asli bersih
    """
    out: List[Tuple[str, str, str]] = []
    seen: set = set()
    link_locators = {
        "google": "div#search a:has(h3), div#rso a:has(h3), a:has(h3)",
        "bing": "li.b_algo h2 a",
        "yahoo": "div.algo h3 a",
    }
    sel = link_locators.get(engine, "a:has(h3)")
    try:
        links = page.locator(sel)
        n = await links.count()
    except Exception:
        return []

    for i in range(min(n, limit * 3)):
        try:
            el = links.nth(i)
            title = (await el.inner_text(timeout=2500) or "").strip()
            title = re.sub(r"\s+", " ", title)
            # Google menempelkan breadcrumb URL ke dalam teks judul
            # (mis. "Inflasi Bank Indonesia https://www.bi.go.id › fungsi-utama").
            # Potong di kemunculan URL pertama biar judulnya bersih.
            title = re.split(r"\s+https?://", title)[0].strip()
            title = re.sub(r"\s+(www\.)?[a-z0-9\-]+\.(id|com|org|net|co|go\.id)\b.*$", "", title).strip()
            if len(title) < 5:
                continue
            raw = await el.get_attribute("href", timeout=2500)
            url = await _resolve_real_url(page, raw or "")
            if not url.startswith("http"):
                # Bing cadangan: pakai <cite> untuk tahu domainnya
                if engine == "bing":
                    continue
                continue
            # buang link internal mesin pencari
            if any(b in url for b in (
                "google.com/", "google.co.id/", "bing.com/", "yahoo.com/",
                "search.brave.com", "duckduckgo.com",
            )):
                continue
            key = title.lower()[:40]
            if key in seen:
                continue
            seen.add(key)
            out.append((title[:150], url, ""))
            if len(out) >= limit:
                break
        except Exception:
            continue
    return out


async def _type_and_search(page: Any, engine: str, query: str) -> str:
    """Buka halaman utama mesin pencari, KETIK query seperti manusia, tekan Enter.
    Mengembalikan teks halaman hasil."""
    cfg = SEARCH_ENGINES[engine]
    await page.goto(cfg["home"], timeout=30000, wait_until="domcontentloaded")
    await asyncio.sleep(0.5)

    box = page.locator(cfg["input"]).first
    await box.wait_for(state="visible", timeout=8000)
    await box.click()
    await asyncio.sleep(0.2)
    # ketik pelan (delay 80-110ms per karakter) — pola ini yang lolos dari deteksi bot
    await box.type(query, delay=90)
    await asyncio.sleep(0.4)
    await page.keyboard.press("Enter")

    # Tunggu hasil BENAR-BENAR ter-render. Dulu cuma sleep 1.6 detik -> Google & Bing
    # belum selesai render, hasil terbaca 0 padahal halamannya sehat (bukan diblokir).
    try:
        await page.wait_for_selector(cfg["result"], timeout=12000)
    except Exception:
        pass
    # beri waktu tambahan supaya semua kartu hasil muncul (Google render bertahap)
    for _ in range(6):
        try:
            n = await page.locator(cfg["result"]).count()
        except Exception:
            n = 0
        if n >= 3:
            break
        await asyncio.sleep(0.5)
    await asyncio.sleep(0.4)

    try:
        return await page.inner_text("body")
    except Exception:
        return ""


async def search_via_browser(
    query: str,
    browser: Any = None,
    limit: int = 5,
    timeout_ms: int = 30000,
    engine: Optional[str] = None,
) -> List[Tuple[str, str, str]]:
    """Cari lewat CHROME SUNGGUHAN: buka mesin pencari, ketik query, Enter, baca hasil.

    Kalau `browser` (CloudBrowserEngine) diberikan, pakai tab bot yang SAMA —
    user benar-benar melihat Chrome-nya bekerja. Kalau tidak, buka Chromium sekali pakai.

    Kalau mesin pertama diblokir, otomatis coba mesin berikutnya (SEARCH_ORDER).
    """
    order = ([engine] if engine in SEARCH_ENGINES else []) + [e for e in SEARCH_ORDER if e != engine]
    LAST_VISIBLE_SEARCH.update({"query": query, "url": "", "engine": "", "count": 0, "tried": []})

    # ---- Jalur 1: pakai tab browser milik bot (TERLIHAT oleh user) ----
    if browser is not None:
        try:
            await browser.ensure_alive()
            page = getattr(browser, "page", None)
            if page and not page.is_closed():
                for eng in order:
                    try:
                        body_txt = await _type_and_search(page, eng, query)
                        LAST_VISIBLE_SEARCH["tried"].append(eng)
                        marker = SEARCH_ENGINES[eng]["block_marker"]
                        if re.search(marker, (body_txt or "")[:1200], re.I):
                            continue
                        out = await _extract_results_from_page(page, eng, limit=limit)
                        if out:
                            LAST_VISIBLE_SEARCH.update({
                                "url": page.url, "engine": eng, "count": len(out),
                            })
                            return out
                    except Exception:
                        continue
        except Exception:
            pass

    # ---- Jalur 2: Chromium sekali pakai (tetap browser nyata) ----
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            b = await pw.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
            )
            ctx = await b.new_context(
                user_agent=_BROWSER_UA, locale="id-ID", viewport={"width": 1366, "height": 800}
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            pg = await ctx.new_page()
            try:
                for eng in order:
                    try:
                        body_txt = await _type_and_search(pg, eng, query)
                        LAST_VISIBLE_SEARCH["tried"].append(eng)
                        marker = SEARCH_ENGINES[eng]["block_marker"]
                        if re.search(marker, (body_txt or "")[:1200], re.I):
                            continue
                        out2 = await _extract_results_from_page(pg, eng, limit=limit)
                        if out2:
                            LAST_VISIBLE_SEARCH.update({
                                "url": pg.url, "engine": eng, "count": len(out2),
                            })
                            return out2
                    except Exception:
                        continue
                return []
            finally:
                await b.close()
    except Exception:
        return []


async def _browser_search_context(query: str, browser: Any = None, limit: int = 5) -> str:
    """Jalankan pencarian lewat Chrome lalu kembalikan blok teks siap disuntik ke konteks AI."""
    rows = await search_via_browser(query, browser=browser, limit=limit)
    if not rows:
        return ""
    eng = SEARCH_ENGINES.get(LAST_VISIBLE_SEARCH.get("engine", ""), {}).get("label", "Chrome")
    lines = [f"- {t} — {u}" for t, u, _ in rows]
    return (
        f"[HASIL PENCARIAN LEWAT CHROME — mesin: {eng}]\n"
        + "\n".join(lines)
    )


async def _ddg_snippets(query: str, limit: int = 5, timeout: float = 7.0) -> List[str]:
    """Ambil POTONGAN TEKS hasil pencarian (bukan hanya URL) dari DuckDuckGo HTML.

    Dipakai untuk data real-time (berita, kenapa sesuatu terjadi, jadwal, skor, cuaca).
    Menggantikan scrape Yahoo yang sudah mati total.
    """
    out: List[str] = []

    # --- Sumber 1: BING (paling tahan blokir dari IP server) ---
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _RESEARCH_UA, "Accept-Language": "id-ID,id;q=0.9,en;q=0.8"},
        ) as c:
            rb = await c.get("https://www.bing.com/search", params={"q": query, "setlang": "id"})
        if rb.status_code == 200:
            for title, _u, snip in _extract_bing_results(rb.text, limit=limit):
                if snip and len(snip) > 40:
                    out.append(f"- {snip[:260]}")
                elif title:
                    out.append(f"- {title[:200]}")
        if len(out) >= 2:
            return out[:limit]
    except Exception:
        pass

    # --- Sumber 2: DuckDuckGo (cadangan; sering balas 202 'anomaly' ke IP datacenter) ---
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _RESEARCH_UA, "Accept-Language": "id-ID,id;q=0.9,en;q=0.8"},
        ) as c:
            r = await c.get("https://html.duckduckgo.com/html/", params={"q": query})
        if r.status_code != 200:
            return out[:limit]
        html_txt = r.text
    except Exception:
        return out[:limit]

    # 1) Ambil blok judul + snippet standar DDG (result__title / result__snippet)
    for m in re.finditer(
        r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|td)>',
        html_txt,
        re.S,
    ):
        clean = _html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) > 40:
            out.append(f"- {clean[:260]}")
        if len(out) >= limit:
            break

    # 2) Kalau markup DDG berubah, jatuh ke judul hasil (tetap lebih baik daripada kosong)
    if not out:
        for title, _url in _extract_result_links(html_txt, limit=limit):
            out.append(f"- {title[:200]}")
    return out[:limit]


async def _search_with_links(query: str, limit: int = 4, timeout: float = 9.0) -> List[Tuple[str, str]]:
    """Cari hasil web dan kembalikan (judul, URL) untuk dibaca penuh.

    Urutan sumber: Bing -> DuckDuckGo. Bing dipakai dulu karena DuckDuckGo kerap
    membalas HTTP 202 "anomaly detected" ke IP datacenter (Railway/cloud), yang bikin
    riset mendadak kosong tanpa error.
    """
    headers = {"User-Agent": _RESEARCH_UA, "Accept-Language": "id-ID,id;q=0.9,en;q=0.8"}

    # --- Sumber 1: Bing ---
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as c:
            rb = await c.get("https://www.bing.com/search", params={"q": query, "setlang": "id"})
        if rb.status_code == 200:
            links = [(t, u) for t, u, _sn in _extract_bing_results(rb.text, limit=limit) if u]
            if links:
                return links
    except Exception:
        pass

    # --- Sumber 2: DuckDuckGo HTML ---
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as c:
            r = await c.get("https://html.duckduckgo.com/html/", params={"q": query})
        if r.status_code != 200:
            return []
        return _extract_result_links(r.text, limit=limit)
    except Exception:
        return []


async def _bing_links(query: str, limit: int = 5, timeout: float = 8.0) -> List[Tuple[str, str]]:
    """Khusus Bing, dipakai jalur riset utama."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _RESEARCH_UA, "Accept-Language": "id-ID,id;q=0.9,en;q=0.8"},
        ) as c:
            r = await c.get("https://www.bing.com/search", params={"q": query, "setlang": "id"})
        if r.status_code != 200:
            return []
        return [(t, u) for t, u, _sn in _extract_bing_results(r.text, limit=limit) if u]
    except Exception:
        return []


# =========================================================================
# TOOL LOOP (#1) — AI boleh MINTA riset berulang sebelum menjawab.
# Beda dengan riset sekali jalan: kalau hasil pertama kurang, dia bisa minta lagi.
# =========================================================================
_TOOL_SCHEMA_HINT = (
    "Kamu sedang dalam MODE RISET. Kamu BOLEH minta gua nyari/baca info dulu sebelum jawab.\n\n"
    "Balas HANYA JSON murni, salah satu dari:\n"
    '1. {"thought":"<alasan singkat 1 kalimat>","tool":"search","query":"<kata kunci pencarian baru>"}\n'
    '2. {"thought":"<alasan>","tool":"read","url":"<URL lengkap http...>"}\n'
    '3. {"thought":"<alasan>","tool":"answer"}   <- kalau data sudah cukup untuk menjawab\n\n'
    "ATURAN:\n"
    "- Maksimal 3 kali minta tool. Jangan berputar-putar.\n"
    "- Pakai 'search' kalau butuh sudut pandang/data baru. Pakai 'read' kalau ada URL yang isinya perlu dibaca utuh.\n"
    "- Kalau data yang ada sudah cukup, pilih 'answer'.\n"
    "- DILARANG mengarang URL. Hanya pakai URL yang muncul di catatan riset.\n"
    "Balas JSON saja, tanpa penjelasan di luar JSON."
)


async def _research_with_tools(
    query: str,
    brain: Any,
    max_rounds: int = 3,
    model_timeout: float = 14.0,
) -> Tuple[str, List[str]]:
    """
    Loop riset bertahap: cari -> baca -> evaluasi -> cari lagi kalau kurang -> jawab.
    Mengembalikan (blok_konteks_untuk_jawaban_final, daftar_jejak_langkah).
    """
    q = (query or "").strip()
    if not q:
        return "", []

    notes: List[str] = []
    trail: List[str] = []
    tried_queries: List[str] = [q.lower()]
    read_urls: List[str] = []

    # RONDE 0 (selalu): riset awal sekali jalan supaya AI punya pijakan
    try:
        base_ctx = await asyncio.wait_for(
            _research_web(q, max_items=6, brain_ref=brain), timeout=40.0
        )
    except Exception:
        base_ctx = ""
    if base_ctx:
        notes.append(base_ctx)
        trail.append("riset awal")

    # Kumpulkan kandidat URL dari hasil awal (kalau ada)
    candidate_urls: List[Tuple[str, str]] = [("", u) for u in list(_LAST_LINKS)[:3]]
    for ln in (base_ctx or "").splitlines():
        if ln.startswith("- ") and "http" in ln:
            m = re.search(r"(https?://[^\s)]+)", ln)
            if m:
                candidate_urls.append(("", m.group(1)))

    for rnd in range(max_rounds):
        # Minta AI memutuskan langkah berikutnya
        decision = ""
        try:
            brief = "\n".join(notes)[:6000]
            ask = (
                f"PERTANYAAN USER: {q}\n\n"
                f"CATATAN RISET SEJAUH INI:\n{brief or '(belum ada)'}\n\n"
                f"URL yang sudah dibaca: {', '.join(read_urls) if read_urls else '(belum ada)'}\n\n"
                "Putuskan langkah berikutnya."
            )
            payload = {
                "system_instruction": {"parts": [{"text": _TOOL_SCHEMA_HINT}]},
                "contents": [{"role": "user", "parts": [{"text": ask}]}],
                "generationConfig": {
                    "temperature": 0.0,
                    "maxOutputTokens": 200,
                    "responseMimeType": "application/json",
                },
            }
            data, _err = await brain._post_gemini_single(
                brain._gemini_candidate_models(light_first=True)[0], payload, timeout=model_timeout
            )
            if data is not None:
                decision = brain._extract_gemini_text(data)
        except Exception:
            decision = ""

        if not decision:
            break

        try:
            parsed = _extract_json(decision)
        except Exception:
            break
        if not isinstance(parsed, dict):
            break

        tool = str(parsed.get("tool") or "").strip().lower()
        if tool in ("answer", "done", "finish", ""):
            trail.append("putuskan jawab")
            break

        if tool == "search":
            nq = str(parsed.get("query") or "").strip()
            if not nq or nq.lower() in tried_queries:
                break
            tried_queries.append(nq.lower())
            trail.append(f"cari: {nq[:40]}")
            try:
                more = await asyncio.wait_for(
                        _research_web(nq, max_items=5, brain_ref=brain), timeout=35.0
                    )
            except Exception:
                more = ""
            if more:
                notes.append(f"--- riset tambahan ({nq[:50]}) ---\n{more}")
                for ln in more.splitlines():
                    if ln.startswith("- ") and "http" in ln:
                        mm = re.search(r"(https?://[^\s)]+)", ln)
                        if mm:
                            candidate_urls.append(("", mm.group(1)))
            continue

        if tool == "read":
            url = str(parsed.get("url") or "").strip()
            if not url.startswith("http") or url in read_urls:
                break
            read_urls.append(url)
            trail.append(f"baca: {url[:40]}")
            txt = await _fetch_page_text(url, max_chars=3500)
            if txt:
                notes.append(f"--- ISI HALAMAN PENUH: {url} ---\n{txt}")
            continue

        break

    # Kalau perjalanan loop tidak menghasilkan tambahan apa pun, coba baca 1 URL dari hasil riset awal
    if not read_urls and candidate_urls:
        for _, u in candidate_urls[:1]:
            txt = await _fetch_page_text(u, max_chars=3000)
            if txt:
                read_urls.append(u)
                trail.append("baca: (otomatis)")
                notes.append(f"--- ISI HALAMAN PENUH: {u} ---\n{txt}")
                break

    # Jaminan: kalau belum ada halaman yang dibaca, baca 1 URL dari jejak riset.
    # (dievaluasi ulang di sini karena candidate_urls terisi selama loop berjalan)
    if not read_urls and candidate_urls:
        for _, u in candidate_urls[:2]:
            txt = await _fetch_page_text(u, max_chars=3000)
            if txt:
                read_urls.append(u)
                trail.append("baca: " + u[:45])
                notes.append(f"--- ISI HALAMAN PENUH: {u} ---\n{txt}")
                break

    if not notes:
        return "", trail

    combined = (
        "=== HASIL RISET BERTAHAP (DATA EKSTERNAL, PAKAI INI SEBAGAI DASAR JAWABAN) ===\n"
        + "\n\n".join(notes)
        + "\n=== AKHIR HASIL RISET ===\n"
    )
    return combined, trail


# =========================================================================
# VALIDASI DOMAIN — supaya bot tidak pernah "menebak" domain yang tidak ada.
# =========================================================================
_DNS_CACHE: Dict[str, Tuple[float, bool]] = {}
_DOMAIN_SYNONYMS = {
    # kata kunci user -> domain resmi yang benar
    "all indonesia": "allindonesia.imigrasi.go.id",
    "allindonesia": "allindonesia.imigrasi.go.id",
    "imigrasi": "imigrasi.go.id",
    "siskopatuh": "siskopatuh.haji.go.id",
    "haji": "haji.go.id",
    "konami": "my.konami.net",
    "efhub": "efootballhub.net",
    "efootball": "efootballhub.net",
    "classroom": "classroom.google.com",
    "drive": "drive.google.com",
    "gmail": "mail.google.com",
    "demoqa": "demoqa.com",
    "saucedemo": "saucedemo.com",
    "toscrape": "books.toscrape.com",
    "wikipedia": "id.wikipedia.org",
    "traveloka": "traveloka.com",
    "tokopedia": "tokopedia.com",
    "shopee": "shopee.co.id",
}


async def _host_is_reachable(url: str, timeout: float = 4.5) -> bool:
    """Cek apakah host benar-benar ada (DNS + HTTP). Hasil di-cache 10 menit."""
    import socket as _socket

    try:
        host = urllib.parse.urlparse(url).netloc.split(":")[0].lower()
    except Exception:
        return False
    if not host:
        return False

    now = _time.monotonic()
    hit = _DNS_CACHE.get(host)
    if hit and (now - hit[0] < 600.0):
        return hit[1]

    ok = False

    # Tahap 1: DNS (cepat, tidak kena blokir anti-bot)
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_socket.gethostbyname, host),
            timeout=min(timeout, 4.0),
        )
        ok = True
    except Exception:
        ok = False

    # Tahap 2: kalau DNS hidup, cek HTTP boleh 403/401 (situs blokir bot itu wajar)
    if ok:
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": _RESEARCH_UA},
            ) as c:
                r = await c.head(url)
                # 403/401/429 tetap berarti HOST ADA
                ok = r.status_code < 500 or r.status_code in (401, 403, 429)
        except Exception:
            ok = True  # DNS hidup -> host ada, walau HTTP rewel

    if len(_DNS_CACHE) > 300:
        _DNS_CACHE.clear()
    _DNS_CACHE[host] = (now, ok)
    return ok


async def _find_official_url(query: str) -> str:
    """
    Cari URL resmi situs lewat mesin pencari yang MASIH HIDUP, lalu validasi DNS-nya.
    Ini pengganti fallback Yahoo Search yang sudah mati (HTTP 500).
    """
    q_low = (query or "").lower()

    # 1) Coba sinonim yang sudah tahu domainnya
    for key, domain in _DOMAIN_SYNONYMS.items():
        if key in q_low:
            url = f"https://{domain}/"
            if await _host_is_reachable(url):
                return url

    # 2) Cari sungguhan di DuckDuckGo, ambil hasil teratas yang valid
    try:
        links = await _search_with_links(query, limit=5)
    except Exception:
        links = []

    for _title, href in links:
        try:
            host = urllib.parse.urlparse(href).netloc.lower()
        except Exception:
            continue
        # Buang agregator/pencarian
        if any(b in host for b in ("duckduckgo", "google.", "bing.", "yahoo.", "facebook.", "pinterest.")):
            continue
        if await _host_is_reachable(href):
            return href
    return ""


def _needs_research(query: str) -> bool:
    q = (query or "").lower().strip()
    if len(q) < 4:
        return False
    # Sapaan / obrolan murni -> tidak perlu riset (biar responsif)
    if q in ("oy", "p", "halo", "hi", "tes", "test", "ping", "bro"):
        return False
    return any(w in q for w in _FRESH_INTENT_WORDS)


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

        # Hanya tarik hasil pencarian jika belum dapat harga finansial ATAU user memang tanya alasan/berita
        # PENTING: pakai DuckDuckGo HTML, BUKAN Yahoo Search (Yahoo scrape sudah mati/berubah markup
        # -> regex compText tidak pernah match, jadi bot seolah "tidak nemu apa-apa").
        if not chunks or any(w in q_low for w in ("berita", "kenapa", "knpa", "jadwal", "skor", "cuaca", "terbaru")):
            snippets = await _ddg_snippets(query, limit=5, timeout=6.0)
            chunks.extend(snippets)
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
3. PENCARIAN WEB: buka HALAMAN UTAMA mesin pencari (`https://www.google.com/`), lalu gunakan action `"type"` pada kolom pencariannya dan tekan Enter — JANGAN langsung `"navigate"` ke URL hasil (`google.com/search?q=...`). Mengetik seperti manusia lolos dari deteksi bot, sedangkan membuka URL hasil langsung memicu "lalu lintas tidak wajar". Jika Google memblokir, ganti ke Bing (`https://www.bing.com/`) dengan cara yang sama. HINDARI DuckDuckGo (202 anomaly), Ecosia (403), Brave (429), dan Startpage (JS challenge) — semuanya memblokir server ini. Jika user hanya menyapa atau bertanya apakah kamu bisa melakukan sesuatu tanpa menyebutkan link/tugas spesifiknya, jawab ramah dengan gaya santai (*gua/lu/bro*) menggunakan `"done"`.
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
        # Engine browser (diisi orchestrator). Kalau ada, pencarian web dikerjakan
        # dengan MEMBUKA mesin pencari di browser nyata, bukan HTTP scrape.
        self.browser: Any = None
        # Callback opsional: dipanggil tiap kali pencarian browser dimulai.
        # Dipakai Telegram bridge untuk menampilkan "🔎 Lagi buka Yahoo di browser...".
        self.search_notice_cb: Any = None
        self._rr_idx: int = 0
        self.last_chat_ok: bool = True
        self.last_error_kind: str = ""
        self.last_error_detail: str = ""
        self.last_research_trail: List[str] = []
        self.last_tier: str = ""
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

    def _gemini_candidate_models(self, *, light_first: bool = False, tier: str = "") -> List[str]:
        """
        3 tingkat model (biar otak maksimal tanpa jebol kuota):
          - "heavy"  : pertanyaan analitis/panjang -> model frontier (Pro) dulu
          - "medium" : obrolan biasa              -> Flash
          - "light"  : sapaan/klasifikasi         -> Flash-Lite (murah & cepat)
        """
        heavy_pool = [
            "gemini-2.5-pro",
            "gemini-3-pro-preview",
            "gemini-2.5-flash",
        ]
        medium_pool = ["gemini-3.6-flash", "gemini-3-flash-preview", "gemini-2.5-flash"]
        light_pool = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]

        if light_first or tier == "light":
            base_list = light_pool + ["gemini-2.5-flash"]
        elif tier == "heavy":
            base_list = heavy_pool + light_pool
        else:
            rot = self._rr_idx % len(medium_pool)
            self._rr_idx += 1
            base_list = (
                medium_pool[rot:]
                + medium_pool[:rot]
                + light_pool
            )

        cfg_m = (self.config.model or "").strip()
        if cfg_m and "flash-lite" not in cfg_m and cfg_m not in base_list:
            base_list.insert(0, cfg_m)

        avail = [m for m in base_list if not self._is_model_temporarily_unavailable(m)]
        return avail or base_list

    @staticmethod
    def _needs_calc(message: str) -> bool:
        """Deteksi apakah user minta hitungan/analisis angka yang wajib akurat."""
        q = (message or "").lower()
        calc_markers = (
            "hitung", "itung", "itungin", "kalkulasi", "simulasi", "proyeksi",
            "bunga majemuk", "compound", "cuan", "return", "persen", "persentase",
            "berapa total", "totalnya", "rata-rata", "average", "median",
            "konversi", "konvert", "kurs", "perbandingan", "rasio", "break even",
            "bep", "amortisasi", "cicilan", "angsuran", "dp ", "tenor",
        )
        if any(w in q for w in calc_markers):
            return True
        # Ada angka + kata finansial = kemungkinan besar butuh hitungan
        has_num = bool(re.search(r"\d", q))
        fin_words = ("juta", "miliar", "ribu", "rupiah", "rp", "dollar", "usd", "tahun", "bulan", "persen", "%")
        return has_num and any(w in q for w in fin_words)

    async def _run_calc(self, message: str, model_timeout: float = 20.0) -> str:
        """
        KALKULATOR NYATA: AI menulis kode Python, sistem mengeksekusi di subprocess terisolasi,
        lalu hasilnya disuntik balik ke konteks jawaban. Jadi angkanya benar, bukan dikira-kira.
        """
        if self.config.provider != "gemini" or not self.config.has_api_key:
            return ""

        code_sys = (
            "Kamu penulis kode Python untuk KALKULASI. Tugasmu: tulis SATU skrip Python yang "
            "menjawab pertanyaan user dengan angka akurat.\n"
            "ATURAN:\n"
            "- HANYA output kode Python murni. Tanpa penjelasan, tanpa markdown fence.\n"
            "- Gunakan hanya modul standar (math, statistics, datetime, json). JANGAN import pandas/numpy.\n"
            "- WAJIB gunakan print() untuk menampilkan hasil akhir yang jelas & berlabel.\n"
            "- Tampilkan angka penting + satuan. Bulatkan wajar (2 desimal untuk uang).\n"
            "- Jangan pakai input(), jangan akses jaringan, jangan baca/tulis file.\n"
            "- Kalau datanya kurang, hitung dengan asumsi eksplisit dan cetak asumsinya."
        )
        try:
            payload = {
                "system_instruction": {"parts": [{"text": code_sys}]},
                "contents": [{"role": "user", "parts": [{"text": message[:2000]}]}],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 800},
            }
            data, _err = await self._post_gemini_single(
                self._gemini_candidate_models(light_first=False, tier="medium")[0],
                payload,
                timeout=model_timeout,
            )
            if data is None:
                return ""
            code = self._extract_gemini_text(data)
        except Exception:
            return ""

        if not code:
            return ""
        # Bersihkan pagar markdown kalau AI bandel
        code = re.sub(r"^```(?:python)?\s*", "", code.strip())
        code = re.sub(r"```\s*$", "", code).strip()
        if "def " in code and "print(" not in code:
            return ""
        if not code:
            return ""

        # Pengaman: tolak kode berbahaya
        blocked = (
            "import os", "import sys", "import subprocess", "import socket", "import shutil",
            "open(", "exec(", "eval(", "__import__", "requests", "urllib", "pathlib",
            "input(", "rm ", "unlink", "remove(", "system(", "popen", "chmod",
        )
        low_code = code.lower()
        if any(b in low_code for b in blocked):
            return ""

        import subprocess as _sp
        import sys as _sys

        try:
            proc = await asyncio.to_thread(
                _sp.run,
                [_sys.executable, "-I", "-c", code],
                capture_output=True,
                text=True,
                timeout=12,
            )
        except Exception:
            return ""

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if not out and not err:
            return ""
        if proc.returncode != 0 and not out:
            return ""

        return (
            "=== HASIL KALKULASI PYTHON (ANGKA NYATA, SUDAH DIHITUNG) ===\n"
            f"{out[:2500]}\n"
            + (f"(catatan: {err[:200]})\n" if err else "")
            + "=== AKHIR HASIL KALKULASI ===\n"
            "_Gunakan angka di atas apa adanya sebagai hasil hitungan final._\n"
        )

    @staticmethod
    def _pick_tier(message: str, has_research: bool = False) -> str:
        """Tentukan kelas model dari bobot pertanyaan."""
        q = (message or "").lower().strip()
        if len(q) < 12:
            return "light"
        heavy_markers = (
            "jelasin", "jelaskan", "analisa", "analisis", "bandingin", "bandingkan", "strategi",
            "rencana", "dampak", "risiko", "kenapa", "mengapa", "bagaimana", "prediksi",
            "proyeksi", "hitung", "simulasi", "evaluasi", "rekomendasi", "menurut lu",
            "pendapat", "opini", "banding", "kelebihan", "kekurangan", "untung", "rugi",
            "investasi", "saham", "portofolio", "bisnis", "karier", "keputusan", "pilih",
        )
        if any(w in q for w in heavy_markers):
            return "heavy"
        if len(q) > 160 or has_research:
            return "heavy"
        return "medium"

    @staticmethod
    def _extract_gemini_text(data: Dict[str, Any]) -> str:
        cands = data.get("candidates") or []
        if not cands:
            return ""
        content = cands[0].get("content") or {}
        parts_out = content.get("parts") or []
        return "".join(str(p.get("text", "")) for p in parts_out if isinstance(p, dict) and not p.get("thought")).strip()

    @staticmethod
    def _gemini_finish_reason(data: Dict[str, Any]) -> str:
        """Ambil finishReason kandidat pertama (STOP / MAX_TOKENS / SAFETY / RECITATION / ...)."""
        cands = data.get("candidates") or []
        if not cands:
            return ""
        return str(cands[0].get("finishReason") or "").strip().upper()

    @staticmethod
    def _looks_truncated(text: str) -> bool:
        """
        Deteksi teks yang berhenti di tengah jalan (kalimat/markdown belum tuntas).
        Sengaja konservatif: lebih baik tidak mendeteksi (lalu tidak menyambung) daripada
        menyambung jawaban yang sebenarnya sudah selesai dan menghasilkan kalimat acak.
        """
        t = (text or "").rstrip()
        if not t:
            return False

        last_line = t.splitlines()[-1].strip() if t.splitlines() else t
        # Baris tabel / bullet / heading / separator markdown yang valid -> bukan terpotong
        if last_line.startswith(("|", "- ", "* ", "#", ">", "```")) or last_line.endswith(("|", "```")):
            return False
        # Diakhiri tanda baca penutup yang wajar -> selesai
        if last_line.endswith((".", "!", "?", ")", "]", "}", '"', "'", "\u201d", "\u2019", "\u00bb")):
            return False
        # Terbuka di tengah penanda markdown tebal/miring/kode/link
        if last_line.count("**") % 2 != 0 or last_line.count("`") % 2 != 0:
            return True
        if last_line.count("[") > last_line.count("]"):
            return True
        # Berhenti di kata sambung / preposisi yang jelas menggantung
        dangling = (
            "dan", "atau", "yang", "untuk", "dengan", "di", "ke", "dari", "pada", "karena",
            "agar", "supaya", "tapi", "tetapi", "serta", "bahwa", "jika", "kalau", "lalu",
            "the", "and", "or", "to", "of", "for", "with", "that", "but",
        )
        last_word = re.split(r"\s+", last_line)[-1].strip(",;:").lower() if last_line else ""
        if last_word in dangling:
            return True
        # Baris terakhir panjang tanpa tanda baca & bukan struktur markdown -> kemungkinan terpotong
        if len(last_line) >= 60:
            return True
        return False

    @staticmethod
    def _friendly_chat_error_message(kind: str) -> str:
        """Pesan error yang kedengeran kayak orang ngomong, bukan log server."""
        if kind == "auth_error":
            return "Gua nggak bisa jawab bro — API key-nya ditolak. Ganti dulu lewat /apikey ya, nanti langsung normal lagi."
        if kind == "rate_limit":
            return "Kuota AI-nya lagi mentok sebentar. Kasih jeda 5–10 detik terus kirim ulang, biasanya langsung masuk."
        if kind == "timeout":
            return "Servernya nggak nyaut dalam batas waktu. Coba kirim ulang aja, kemungkinan besar lagi lelet sebentar."
        if kind == "network_error":
            return "Koneksi gua ke server AI keputus di tengah jalan. Kirim ulang pesannya ya, ini biasanya cuma sementara."
        return "Server AI-nya lagi rame banget bro. Tunggu beberapa detik terus kirim ulang, gua langsung jawab."

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

        # KONTEKS PANJANG: 30 pesan terakhir x 1500 karakter (~45k char) — bukan cuma 10 x 400.
        # Ini yang bikin bot inget obrolan jauh ke belakang, kayak agent beneran.
        hist_lines = []
        for m in (recent_chat or [])[-30:]:
            r = m.get("role", "")
            if r in ("user", "agent"):
                lbl = "USER" if r == "user" else "KAMU"
                body = (m.get("text") or "").strip()[:1500]
                if body:
                    hist_lines.append(f"{lbl}: {body}")
        hist_block = (
            "=== RIWAYAT OBROLAN SEBELUMNYA (pakai ini biar nyambung, jangan ngulang pertanyaan) ===\n"
            + "\n".join(hist_lines)
            + "\n=== AKHIR RIWAYAT ===\n\n"
        ) if hist_lines else ""

        live_ctx = await _fetch_live_web_context(message)

        # RISET WEB BERTAHAP (#1 + #2): cari -> baca halaman penuh -> kalau kurang, cari lagi.
        # Ini yang bikin dia bisa "ngejar" informasi, bukan cuma sekali cari lalu ngarang.
        research_ctx = ""
        self.last_research_trail = []
        if _needs_research(message):
            # Kabari UI bahwa pencarian web SEDANG BERJALAN DI BROWSER (transparan, bukan diam-diam)
            if callable(self.search_notice_cb):
                try:
                    await self.search_notice_cb(message, _yahoo_url(message))
                except Exception:
                    pass
            try:
                research_ctx, trail = await asyncio.wait_for(
                    _research_with_tools(message, self, max_rounds=3),
                    timeout=75.0,
                )
                self.last_research_trail = list(trail)
            except Exception:
                # Fallback: riset sekali jalan kalau loop gagal/timeout
                try:
                    research_ctx = await asyncio.wait_for(
                        _research_web(message, brain_ref=self), timeout=50.0
                    )
                except Exception:
                    research_ctx = ""

        # KALKULATOR: kalau user minta hitungan, jalankan Python DULU biar angkanya akurat.
        calc_ctx = ""
        if self._needs_calc(message):
            try:
                calc_ctx = await asyncio.wait_for(self._run_calc(message), timeout=25.0)
            except Exception:
                calc_ctx = ""

        mem_part = (user_memory_block.strip() + "\n\n") if user_memory_block else ""
        chat_sys = (
            "Kamu adalah agent AI pribadi milik user, jalan di Telegram. Gaya ngomong: santai, hangat, dan to-the-point "
            "kayak temen senior yang ngerti teknis — pakai 'gua/lu/bro' secukupnya, JANGAN kaku & JANGAN berbunga-bunga.\n"
            "TAPI kalau ada blok 'PERSONA & GAYA BAHASA YANG DIPILIH USER', IKUTI PERSIS itu — "
            "itu instruksi user, menang di atas gaya default.\n\n"

            "ATURAN JAWAB (WAJIB):\n"
            "1. SELALU jawab pertanyaannya dulu. Jangan pernah balas kosong, jangan pernah cuma 'oke', "
            "jangan pernah nunda jawaban ('nanti gua jelasin'), jangan pernah nanya balik kalau sebenarnya bisa dijawab.\n"
            "2. Panjang jawaban MENGIKUTI kebutuhan, bukan dipotong demi 'ringkas'. Pertanyaan simpel -> jawab simpel. "
            "Pertanyaan berat -> boleh panjang & terstruktur (poin-poin), asal tiap kalimat ada isinya. "
            "Jangan pernah ngecilin jawaban cuma biar keliatan padat.\n"
            "3. Format Telegram: pakai *bold* satu bintang, dan bullet '•'. JANGAN pakai tabel markdown '|' "
            "dan JANGAN pakai heading '#' karena jelek di HP. Jangan pakai **dua bintang**.\n"
            "4. Kalau user nanya soal kondisinya/uang/investasi/pilihan hidup: jawab konkret dengan angka & langkah nyata, "
            "boleh kasih opini jujur + alasan. Jangan muter-muter atau cuma nyuruh 'konsultasi ke ahlinya'.\n"
            "5. JANGAN NGARANG. Kalau ada 'HASIL RISET WEB', pakai itu sebagai dasar utama jawaban lu — sebut angkanya "
            "persis seperti di sumber. Kalau riset kosong dan kamu nggak yakin, bilang jujur 'ini gua nggak tau pasti' "
            "lalu kasih yang kamu yakin + cara ngeceknya. DILARANG ngarang angka, tanggal, nama, atau link.\n"
            "5b. Kalau jawabanmu pakai hasil riset, akhiri dengan 1 baris sumber: 'Sumber: <nama sumber yang relevan>'. "
            "Kalau pakai data pasar real-time, sebut itu data live.\n"
            "6. Kalau user cuma nyapa singkat ('oy', 'p', 'halo'), balas singkat & hangat, lalu tawarin bantuan konkret.\n"
            "7. NYAMBUNG SAMA RIWAYAT: baca 'RIWAYAT OBROLAN SEBELUMNYA' dulu. Kalau user pakai kata rujukan "
            "('itu', 'tadi', 'yang tadi', 'lanjut', 'terus'), sambungkan ke topik yang lagi dibahas — JANGAN nanya "
            "'maksudnya apa' kecuali benar-benar ambigu.\n"
            "8. Kalau user lagi curhat/ngobrol panjang, ikuti alurnya kayak temen ngobrol beneran: tanggepin poinnya, "
            "kasih opini, baru tawarin langkah. Jangan balas seperti template.\n"
            "9. KEDALAMAN: kalau topiknya teknis/analitis (investasi, kesehatan, hukum, teknologi, keputusan besar), "
            "jangan jawab 1-2 baris. Kasih: (a) intinya dulu, (b) alasan/angka pendukung, (c) risiko atau sisi lemahnya, "
            "(d) langkah konkret yang bisa dilakukan. Kayak analis senior yang lagi njelasin ke kolega — bukan customer service.\n"
            "10. KALAU ADA RISIKO: selalu sebutin downside/kemungkinan buruknya, jangan cuma jual mimpi. "
            "Kalau datanya nggak cukup buat nyimpulin, bilang butuh data apa.\n\n"

            "ATURAN ANTI-HALUSINASI:\n"
            "• Di balasan ini kamu HANYA ngobrol teks — TIDAK ada proses background yang jalan. "
            "DILARANG nulis 'lagi gua proses...', 'tunggu bentar...', '[Laporan Hasil...]', atau link palsu 'sandbox:/mnt/data/...'.\n"
            "• Kalau user minta isi dokumen/surat/tabel, tulis langsung isinya dengan rapi — sistem otomatis "
            "yang bikin file aslinya, jadi jangan bikin link download sendiri.\n"
            "• Kalau ada 'DATA INTERNET REAL-TIME DETIK INI', pakai angkanya persis, jangan dibulatkan ngawur.\n"
            "• Kalau ada 'MEMORI PROFIL & GAYA KERJA PEMILIK BOT', pakai otomatis buat surat/CV/dokumen "
            "dan pertanyaan tentang user — jangan nanya ulang data yang udah ada.\n"
            "• User nggak perlu nekan tombol mode apa pun: cukup ketik bebas, kamu yang nentuin sendiri.\n\n"

            "KEMAMPUAN ASLI BOT INI (sebut kalau relevan, jangan promosi terus):\n"
            "buka & kendalikan browser server, isi form, login, scraping/rangkum web, bikin file Word/Excel/PDF, "
            "ingat profil user (/ingat), dan kontrol layar live."
        )
        prompt = f"{mem_part}{hist_block}{live_ctx}{research_ctx}{calc_ctx}USER: {message}"

        async with self._llm_semaphore:
            if self.config.provider == "gemini":
                # Routing otomatis: pertanyaan berat -> model frontier (Pro), ringan -> Flash/Lite
                tier = self._pick_tier(message, has_research=bool(research_ctx or calc_ctx))
                self.last_tier = tier
                fast_order = self._gemini_candidate_models(light_first=False, tier=tier)
                backoff_step = 0
                last_err: Optional[LLMCallError] = None

                # Semua kandidat tetap dicoba (resiliensi), TAPI dibatasi deadline total ~14 detik
                # supaya bot nggak kelihatan "diem" berjam-jam saat provider sedang gangguan.
                deadline_chat = _time.monotonic() + (75.0 if tier == "heavy" else 30.0)
                for m_name in fast_order:
                    if _time.monotonic() >= deadline_chat and last_err is not None:
                        break
                    # CATATAN: maxOutputTokens dinaikkan dari 1200 -> 4096 supaya jawaban panjang
                    # tidak terpotong di tengah kalimat. thinkingConfig dimatikan karena pada model
                    # thinking, token "mikir" ikut memakan kuota output -> jawaban jadi setengah.
                    # Heavy (Pro) dapat ruang token lebih besar untuk analisis mendalam.
                    _is_heavy = tier == "heavy"
                    gen_cfg: Dict[str, Any] = {
                        "temperature": 0.4,
                        "maxOutputTokens": 8192 if _is_heavy else 4096,
                    }
                    # Model Lite/Flash: matikan thinking supaya token penuh untuk teks.
                    # Model Pro: BIARKAN thinking default (itu yang bikin nalarnya dalam).
                    if not _is_heavy:
                        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
                    payload = {
                        "system_instruction": {"parts": [{"text": chat_sys}]},
                        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                        "generationConfig": gen_cfg,
                    }
                    data, err = await self._post_gemini_single(m_name, payload, timeout=55.0 if _is_heavy else 30.0)
                    if data is not None:
                        ans = self._extract_gemini_text(data)
                        finish = self._gemini_finish_reason(data)
                        if ans:
                            # AUTO-CONTINUATION: kalau AI kehabisan token atau berhenti di tengah kalimat,
                            # sambung otomatis (maks 2 putaran) supaya jawaban utuh sampai selesai.
                            cont_round = 0
                            while cont_round < 2 and (
                                finish == "MAX_TOKENS"
                                or (len(ans) >= 180 and self._looks_truncated(ans))
                            ):
                                cont_round += 1
                                cont_prompt = (
                                    "JAWABANMU SEBELUMNYA TERPOTONG DI TENGAH. "
                                    "Lanjutkan LANGSUNG dari kata terakhir tanpa mengulang kalimat sebelumnya, "
                                    "tanpa sapaan, tanpa penjelasan tambahan. Tulis lanjutannya saja sampai tuntas:\n\n"
                                    f"{ans[-1200:]}"
                                )
                                cont_payload = {
                                    "system_instruction": {"parts": [{"text": chat_sys}]},
                                    "contents": [{"role": "user", "parts": [{"text": prompt + "\n\n" + cont_prompt}]}],
                                    "generationConfig": dict(gen_cfg),
                                }
                                data_c, err_c = await self._post_gemini_single(m_name, cont_payload, timeout=25.0)
                                if data_c is None:
                                    break
                                more_txt = self._extract_gemini_text(data_c)
                                if not more_txt:
                                    break
                                ans = ans.rstrip() + "\n" + more_txt.lstrip()
                                finish = self._gemini_finish_reason(data_c)
                                if finish not in ("MAX_TOKENS", "") and not self._looks_truncated(ans):
                                    break

                            self.last_chat_ok = True
                            self.last_error_kind = ""
                            self.last_error_detail = ""
                            return ans
                        err = LLMCallError(
                            f"Respons teks kosong dari {m_name} (finishReason: {finish or 'UNKNOWN'})",
                            kind="invalid_output",
                            model_name=m_name,
                        )

                    last_err = err
                    if err and err.kind == "auth_error":
                        break
                    if err and err.kind in ("model_not_found", "bad_request", "invalid_output"):
                        continue
                    if err and err.kind in ("rate_limit", "server_overloaded", "timeout", "network_error"):
                        delay = compute_backoff_delay(backoff_step, retry_after=err.retry_after, base=0.6, cap=4.5)
                        backoff_step += 1
                        # Jangan pernah tidur melewati deadline: langsung lempar hasil akhir ke user.
                        remaining = deadline_chat - _time.monotonic()
                        if remaining > 0:
                            await self._sleep_fn(min(delay, remaining))

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
                                "max_tokens": 4096,
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
                                "max_tokens": 4096,
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

        gen_doc: Dict[str, Any] = {
            "temperature": 0.25,
            "maxOutputTokens": 4096,
            "thinkingConfig": {"thinkingBudget": 0},
        }
        payload = {
            "system_instruction": {"parts": [{"text": sys_txt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen_doc,
        }
        async with self._llm_semaphore:
            last_err: Optional[LLMCallError] = None
            backoff_idx = 0
            for model in self._gemini_candidate_models(light_first=False)[:4]:
                data, err = await self._post_gemini_single(model, payload, timeout=30.0)
                if data is not None:
                    txt_out = self._extract_gemini_text(data)
                    finish_d = self._gemini_finish_reason(data)
                    if txt_out:
                        # Sambung otomatis bila rangkuman dokumen kehabisan token
                        cont_d = 0
                        while cont_d < 2 and (
                            finish_d == "MAX_TOKENS"
                            or (len(txt_out) >= 180 and self._looks_truncated(txt_out))
                        ):
                            cont_d += 1
                            cont_payload_d = {
                                "system_instruction": {"parts": [{"text": sys_txt}]},
                                "contents": [
                                    {
                                        "role": "user",
                                        "parts": parts
                                        + [
                                            {
                                                "text": (
                                                    "\n\nJAWABANMU SEBELUMNYA TERPOTONG. Lanjutkan LANGSUNG dari kata "
                                                    "terakhir tanpa mengulang, tulis lanjutannya saja sampai tuntas:\n"
                                                    + txt_out[-1200:]
                                                )
                                            }
                                        ],
                                    }
                                ],
                                "generationConfig": dict(gen_doc),
                            }
                            data_d, _err_d = await self._post_gemini_single(model, cont_payload_d, timeout=30.0)
                            if data_d is None:
                                break
                            more_d = self._extract_gemini_text(data_d)
                            if not more_d:
                                break
                            txt_out = txt_out.rstrip() + "\n" + more_d.lstrip()
                            finish_d = self._gemini_finish_reason(data_d)
                            if finish_d not in ("MAX_TOKENS", "") and not self._looks_truncated(txt_out):
                                break

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
        """
        Ubah permintaan jadi URL resmi — TAPI divalidasi dulu supaya bot tidak menebak domain ngawur.
        Urutan: URL eksplisit -> domain eksplisit (diuji DNS) -> situs dikenal -> tebakan AI (diuji DNS)
        -> pencarian web (DuckDuckGo, bukan Yahoo yang sudah mati).
        """
        q = (raw_query or "").strip()
        if q.startswith(("http://", "https://", "/demo-portal")):
            return q
        if "." in q and " " not in q:
            candidate = f"https://{q}"
            if await _host_is_reachable(candidate):
                return candidate
            # domain eksplisit tapi mati -> lanjut cari yang benar di bawah

        q_low = q.lower()

        # Situs yang sudah pasti (tidak perlu tebak)
        KNOWN = {
            "siskopatuh": "https://siskopatuh.haji.go.id/web/",
            "all indonesia": "https://allindonesia.imigrasi.go.id/",
            "allindonesia": "https://allindonesia.imigrasi.go.id/",
            "imigrasi": "https://imigrasi.go.id/",
            "konami": "https://my.konami.net",
            "efhub": "https://efootballhub.net",
            "classroom": "https://classroom.google.com",
        }
        for key, url in KNOWN.items():
            if key in q_low and await _host_is_reachable(url):
                return url

        # Tebakan AI — WAJIB divalidasi DNS sebelum dipakai
        if self.config.has_api_key and self.config.provider == "gemini":
            payload = {
                "system_instruction": {
                    "parts": [
                        {
                            "text": (
                                "Ubah permintaan website dari user menjadi SATU URL resmi HTTPS yang paling tepat. "
                                "Contoh: 'efhub' -> 'https://efootballhub.net', 'Konami ID' -> 'https://my.konami.net', "
                                "'SISKOPATUH' -> 'https://siskopatuh.haji.go.id/web/', 'Google Classroom' -> 'https://classroom.google.com'. "
                                "PENTING: jika tidak 100% yakin URL resminya, balas dengan {\"url\": \"\"} (kosong) — "
                                "JANGAN menebak domain karangan. "
                                "Balas HANYA JSON: {\"url\": \"https://...\"}"
                            )
                        }
                    ]
                },
                "contents": [{"role": "user", "parts": [{"text": q}]}],
                "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
            }
            for model in self._gemini_candidate_models(light_first=True)[:3]:
                data, err = await self._post_gemini_single(model, payload, timeout=6.0)
                if data is not None:
                    try:
                        parsed = _extract_json(self._extract_gemini_text(data))
                        cand = str(parsed.get("url") or "").strip()
                        if cand.startswith("http") and await _host_is_reachable(cand):
                            return cand
                    except Exception:
                        continue
                if err and err.kind == "auth_error":
                    break

        # JALAN TERAKHIR: cari situs resminya lewat mesin pencari yang masih hidup
        found = await _find_official_url(q)
        if found:
            return found

        return f"https://duckduckgo.com/?q={urllib.parse.quote_plus(q)}"

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
                data, err = await self._post_gemini_single(m_name, payload, timeout=15.0)
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
