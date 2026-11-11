import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

# Pola kredensial umum yang WAJIB disensor dari log, task state, pesan error, dan riwayat chat
_SECRET_PATTERNS = [
    # Token Bot Telegram (contoh: 123456789:AA...)
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,55}\b"), "[REDACTED_TG_TOKEN]"),
    # URL Telegram Bot API lengkap
    (re.compile(r"(https?://api\.telegram\.org/(?:file/)?bot)[^/\s\"']+", re.I), r"\1[REDACTED_TG_TOKEN]"),
    # Google / Gemini API Keys
    (re.compile(r"\bAIza[0-9A-Za-z_-]{28,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bAQ\.[0-9A-Za-z._-]{24,}\b"), "[REDACTED_API_KEY]"),
    # OpenAI / Anthropic / OpenRouter / HuggingFace Keys
    (re.compile(r"\bsk-(?:proj-|ant-|or-)?[A-Za-z0-9_-]{18,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"), "[REDACTED_API_KEY]"),
    # Query parameter rahasia (?key=..., &api_key=..., &token=...)
    (re.compile(r"([?&](?:key|api_key|apikey|token|access_token|secret|password|pin)=)[^&\s\"']+", re.I), r"\1[REDACTED]"),
    # Basic auth pada URL proxy/http (http://user:pass@host)
    (re.compile(r"((?:https?|socks5)://[^:@/\s\"']+:)[^@/\s\"']+(@)", re.I), r"\1[REDACTED]\2"),
    # Pasangan eksplisit password/pin/otp/sandi di teks
    (
        re.compile(
            r"(\b(?:password|kata\s*sandi|sandi|passcode|otp|pin|api[_\s]?key|secret[_\s]?token)\s*[:=]\s*)([^\s,;\"'\n]+)",
            re.I,
        ),
        r"\1[REDACTED_SECRET]",
    ),
]

_DYNAMIC_SECRETS: Set[str] = set()


def register_runtime_secret(secret_val: Optional[str]) -> None:
    """Mendaftarkan nilai kredensial aktif di memori agar selalu disensor bila muncul di string mana pun."""
    s = (secret_val or "").strip()
    if len(s) >= 6:
        _DYNAMIC_SECRETS.add(s)


def _collect_env_secrets() -> Set[str]:
    found = set(_DYNAMIC_SECRETS)
    for env_k in (
        "TELEGRAM_BOT_TOKEN",
        "GEMINI_API_KEY",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "BROWSER_PROXY_URL",
        "WEB_ACCESS_PIN",
    ):
        val = os.getenv(env_k, "").strip()
        if len(val) >= 6:
            found.add(val)
    return found


def redact_secrets(text: Any, extra_secrets: Iterable[str] = ()) -> str:
    """Menyensor seluruh token, API key, password, PIN, dan kredensial proxy dari teks."""
    if text is None:
        return ""
    out = str(text)
    if not out:
        return out

    for dyn in sorted(_collect_env_secrets().union({s for s in extra_secrets if s and len(s) >= 6}), key=len, reverse=True):
        if dyn in out:
            out = out.replace(dyn, "[REDACTED]")

    for rx, repl in _SECRET_PATTERNS:
        out = rx.sub(repl, out)
    return out


def redact_structure(data: Any, extra_secrets: Iterable[str] = ()) -> Any:
    """Menyensor kredensial secara rekursif pada dict/list sebelum disimpan ke disk/state."""
    if isinstance(data, str):
        return redact_secrets(data, extra_secrets=extra_secrets)
    if isinstance(data, list):
        return [redact_structure(v, extra_secrets=extra_secrets) for v in data]
    if isinstance(data, tuple):
        return [redact_structure(v, extra_secrets=extra_secrets) for v in data]
    if isinstance(data, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in data.items():
            kl = str(k).lower()
            if any(sk in kl for sk in ("password", "api_key", "apikey", "bot_token", "secret_token", "access_token")):
                cleaned[k] = "[REDACTED]" if v else ""
            else:
                cleaned[k] = redact_structure(v, extra_secrets=extra_secrets)
        return cleaned
    return data


# Pola skrip destruktif atau berbahaya pada run_python / run_js
_BLOCKED_EXFIL_RE = re.compile(
    r"\b(TELEGRAM_BOT_TOKEN|GEMINI_API_KEY|LLM_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|WEB_ACCESS_PIN|security_state\.json)\b"
    r"|os\.environ",
    re.I,
)

_DESTRUCTIVE_PY_RE = re.compile(
    r"\b(os\.remove|os\.unlink|os\.rmdir|os\.removedirs|shutil\.rmtree|shutil\.move|subprocess\.|os\.system|os\.popen)\b"
    r"|\.unlink\s*\(|\.rmdir\s*\(",
    re.I,
)

_DESTRUCTIVE_JS_RE = re.compile(
    r"\b(localStorage\.clear|sessionStorage\.clear|indexedDB\.deleteDatabase)\b|document\.cookie\s*=",
    re.I,
)


def inspect_script_safety(code: str, lang: str = "python") -> Dict[str, Any]:
    """
    Memeriksa keamanan kode sebelum dieksekusi oleh Agent:
    - 'block': mencoba membaca kredensial/env rahasia server (ditolak mutlak).
    - 'approve': mengandung operasi penghapusan file/direktori/subproses (wajib persetujuan manusia).
    - 'safe': aman dijalankan di dalam sandbox workspace.
    """
    src = (code or "").strip()
    if not src:
        return {"decision": "safe"}

    if _BLOCKED_EXFIL_RE.search(src):
        return {
            "decision": "block",
            "category": "Proteksi Kredensial Sistem",
            "reason": "Skrip ditolak karena mencoba mengakses environment variable atau kredensial rahasia server.",
        }

    if lang == "python":
        m_dest = _DESTRUCTIVE_PY_RE.search(src)
        if m_dest:
            return {
                "decision": "approve",
                "category": "Eksekusi Skrip Destruktif / Sistem",
                "target_label": f"run_python ({m_dest.group(0)})",
                "reason": f"Skrip Python mengandung operasi penghapusan/sistem ('{m_dest.group(0)}') yang wajib disetujui pemilik.",
            }
    elif lang == "js":
        m_js = _DESTRUCTIVE_JS_RE.search(src)
        if m_js:
            return {
                "decision": "approve",
                "category": "Modifikasi Storage / Sesi Browser",
                "target_label": f"run_js ({m_js.group(0)})",
                "reason": f"Skrip JS mencoba menghapus sesi/storage browser ('{m_js.group(0)}') dan memerlukan izin pemilik.",
            }

    return {"decision": "safe"}


class PersistentSecurityState:
    """
    Menyimpan kunci pemilik (owner_id), PIN akses web, dan webhook_secret_token secara atomik
    dengan izin file ketat (0600) agar penguncian keamanan TIDAK PERNAH terbuka kembali saat server restart.
    """

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).resolve()
        self.file_path = self.state_dir / "security_state.json"

    def load(self) -> Dict[str, Any]:
        try:
            if self.file_path.exists():
                data = json.loads(self.file_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def save(self, owner_id: Optional[int], owner_label: str, web_access_key: str, webhook_secret: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "owner_id": owner_id,
            "owner_label": owner_label or "",
            "web_access_key": web_access_key,
            "webhook_secret": webhook_secret,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".sec_", suffix=".tmp", dir=str(self.state_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, str(self.file_path))
        except Exception:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except Exception:
                pass

    @staticmethod
    def generate_webhook_secret() -> str:
        return secrets.token_urlsafe(32)
