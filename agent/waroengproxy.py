"""Integrasi WaroengProxy: kelola akun tanpa perlu buka dashboard.

Kenapa modul ini ada
--------------------
WaroengProxy sengaja TIDAK memakai email/nomor HP. Identitas akun = satu
"token akses" acak yang hanya ditampilkan SEKALI saat mendaftar, dan
token itu tidak bisa dipulihkan (lihat waroengproxy.com/blog/keamanan-akun).
Akibatnya kalau token hilang, akun + saldo + kuota ikut hilang.

Modul ini menyelesaikan masalah itu dengan dua cara:

1. TOKEN DISIMPAN DI VOLUME PERSISTEN (/data), bukan di chat.
   Terminal (Railway) membaca env var `WX_TOKEN`. Kalau tidak ada, token
   diambil dari file di profile dir yang ikut ter-backup.

2. API KEY dipakai untuk mengambil KREDENSIAL PROXY kapan saja.
   Sesuai dokumentasi: GET /api/v1/proxy mengembalikan host, port,
   username, password. Jadi password proxy tidak perlu diingat manusia.

Tidak ada satu pun nilai rahasia yang ditulis ke log atau dikirim ke AI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # httpx sudah dipakai proyek ini; fallback ke requests kalau tidak ada
    import httpx as _http
except Exception:  # pragma: no cover
    _http = None  # type: ignore[assignment]


BASE_URL = "https://waroengproxy.com/api/v1"
ENV_TOKEN = "WX_TOKEN"
ENV_API_KEY = "WX_API_KEY"
ENV_API_KEY_FILE = "WX_API_KEY_FILE"
TOKEN_FILE_NAME = "waroengproxy_token.txt"


class WaroengProxyError(RuntimeError):
    """Kesalahan yang pesannya aman ditampilkan ke pengguna."""


class WaroengProxy:
    """Klien tipis untuk REST API WaroengProxy v1.

    Parameter
    ---------
    profile_dir:
        Direktori persisten (Railway Volume). Token akses & API key
        disimpan di sini supaya ikut ter-backup.
    api_key:
        Kalau diisi, menang atas file & env var. Berguna untuk pengujian.
    """

    def __init__(self, profile_dir: str | Path, api_key: Optional[str] = None):
        self.profile_dir = Path(profile_dir)
        self._api_key_override = (api_key or "").strip() or None

    # ------------------------------------------------------------------ #
    # Penyimpanan rahasia
    # ------------------------------------------------------------------ #
    @property
    def token_file(self) -> Path:
        return self.profile_dir / TOKEN_FILE_NAME

    @property
    def api_key_file(self) -> Path:
        return self.profile_dir / "waroengproxy_apikey.txt"

    def save_token(self, token: str) -> Path:
        """Simpan token akses ke volume persisten (chmod 600 bila didukung)."""
        token = (token or "").strip()
        if not token:
            raise WaroengProxyError("Token kosong, tidak ada yang disimpan.")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(token, encoding="utf-8")
        try:
            os.chmod(self.token_file, 0o600)
        except Exception:
            pass
        return self.token_file

    def load_token(self) -> Optional[str]:
        """Ambil token: env var dulu, lalu file di volume."""
        env_token = (os.getenv(ENV_TOKEN) or "").strip()
        if env_token:
            return env_token
        try:
            if self.token_file.exists():
                saved = self.token_file.read_text(encoding="utf-8").strip()
                return saved or None
        except Exception:
            pass
        return None

    def save_api_key(self, key: str) -> Path:
        key = (key or "").strip()
        if not key:
            raise WaroengProxyError("API key kosong, tidak ada yang disimpan.")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.api_key_file.write_text(key, encoding="utf-8")
        try:
            os.chmod(self.api_key_file, 0o600)
        except Exception:
            pass
        return self.api_key_file

    def load_api_key(self) -> Optional[str]:
        if self._api_key_override:
            return self._api_key_override
        for name in (ENV_API_KEY, ENV_API_KEY_FILE):
            val = (os.getenv(name) or "").strip()
            if not val:
                continue
            if name == ENV_API_KEY_FILE:
                try:
                    val = Path(val).read_text(encoding="utf-8").strip()
                except Exception:
                    continue
            if val:
                return val
        try:
            if self.api_key_file.exists():
                saved = self.api_key_file.read_text(encoding="utf-8").strip()
                return saved or None
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 20.0,
    ) -> Any:
        api_key = self.load_api_key()
        if not api_key:
            raise WaroengProxyError(
                "Belum ada API key WaroengProxy. Buat di dashboard menu "
                "'Akses API', lalu simpan dengan perintah /wxkey."
            )
        url = f"{BASE_URL}{path}"
        headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}

        if _http is None:  # pragma: no cover - httpx wajib ada di requirements
            raise WaroengProxyError("Klien HTTP tidak tersedia (httpx tidak terpasang).")

        try:
            with _http.Client(timeout=timeout) as client:
                resp = client.request(method, url, headers=headers, json=payload)
        except Exception as exc:
            raise WaroengProxyError(f"Gagal menghubungi WaroengProxy: {str(exc)[:160]}") from exc

        if resp.status_code >= 400:
            # API selalu mengembalikan {"error": "..."} dalam Bahasa Indonesia
            try:
                detail = resp.json().get("error") or resp.text[:300]
            except Exception:
                detail = resp.text[:300]
            raise WaroengProxyError(f"HTTP {resp.status_code}: {detail}")

        try:
            return resp.json()
        except Exception as exc:
            raise WaroengProxyError("Balasan WaroengProxy bukan JSON yang valid.") from exc

    # ------------------------------------------------------------------ #
    # Endpoint
    # ------------------------------------------------------------------ #
    def account(self) -> Dict[str, Any]:
        """Saldo, kuota GB, rank, add-on."""
        return self._request("GET", "/account")

    def proxy_credentials(self) -> Dict[str, Any]:
        """host, port, username, password, whitelist."""
        return self._request("GET", "/proxy")

    def regenerate_password(self) -> Dict[str, Any]:
        """Buat password proxy baru (username tetap)."""
        return self._request("POST", "/proxy/regenerate")

    def buy_gb(self, gb: float) -> Dict[str, Any]:
        """Tukar saldo jadi kuota GB."""
        return self._request("POST", "/plans/buy", {"gb": gb})

    def create_qris_deposit(self, amount: int) -> Dict[str, Any]:
        """Buat tagihan QRIS. Balasan berisi id, pay_url, qris, expires_at."""
        if int(amount) < 10000:
            raise WaroengProxyError("Deposit minimal Rp10.000.")
        return self._request("POST", "/deposits", {"method": "qris", "amount": int(amount)})

    def deposits(self) -> List[Dict[str, Any]]:
        """Riwayat deposit, terbaru dulu."""
        data = self._request("GET", "/deposits")
        return data if isinstance(data, list) else []

    def transactions(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/transactions")
        return data if isinstance(data, list) else []

    def usage(self, days: int = 14) -> List[Dict[str, Any]]:
        days = max(1, min(90, int(days)))
        data = self._request("GET", f"/usage?days={days}")
        return data if isinstance(data, list) else []

    def whitelist_add(self, ip: str) -> Dict[str, Any]:
        return self._request("POST", "/proxy/whitelist", {"ip": ip})

    def whitelist_remove(self, ip: str) -> Dict[str, Any]:
        return self._request("POST", "/proxy/whitelist/remove", {"ip": ip})

    # ------------------------------------------------------------------ #
    # Helper tingkat tinggi
    # ------------------------------------------------------------------ #
    def ready_proxy_code(self, country: str = "ID", session: str = "bot01", ttl: int = 3600) -> str:
        """Rakit kode siap tempel untuk parser bot.

        Format yang dihasilkan sengaja memakai pemisah ':' (bukan '@')
        supaya password yang mengandung '@' atau ':' tidak merusak URL.
        """
        creds = self.proxy_credentials()
        host = creds.get("host") or "proxy.waroengproxy.com"
        port = int(creds.get("port") or 8000)
        user = (creds.get("username") or "").strip()
        password = creds.get("password") or ""
        if not user or not password:
            raise WaroengProxyError(
                "WaroengProxy tidak mengembalikan username/password. "
                "Cek akun di dashboard."
            )
        user = f"{user}-cc-{country}-s-{session}-ttl-{int(ttl)}"
        return f"{host}:{port}:{user}:{password}"

    def snapshot(self) -> Dict[str, Any]:
        """Ringkasan untuk laporan ke Telegram (tanpa membocorkan password)."""
        acct = self.account()
        gb = acct.get("gb") or {}
        return {
            "id": acct.get("id"),
            "balance": acct.get("balance"),
            "gb_purchased": gb.get("purchased"),
            "gb_left": gb.get("left"),
            "gb_used": gb.get("used"),
            "created_at": acct.get("created_at"),
        }


def format_rupiah(amount: Any) -> str:
    try:
        return "Rp" + f"{int(amount):,}".replace(",", ".")
    except Exception:
        return "Rp?"


def render_qris_png(payload: str, target: str | Path) -> Optional[Path]:
    """Render string QRIS jadi PNG. Balikkan None kalau library tidak ada."""
    try:
        import qrcode  # noqa: WPS433 (import lokal, opsional)
    except Exception:
        return None
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    img = qrcode.make(payload)
    img.save(target)
    return target
