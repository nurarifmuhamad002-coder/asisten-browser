import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Set, Tuple

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


class StorageUnavailableError(RuntimeError):
    """Dilempar saat penulisan state penting diblokir karena penyimpanan persisten bermasalah."""


class PersistentSecurityState:
    """
    Menyimpan kunci pemilik (owner_id), PIN akses web, dan webhook_secret_token secara atomik
    dengan izin file ketat (0600) agar penguncian keamanan TIDAK PERNAH terbuka kembali saat server restart.
    """

    def __init__(self, state_dir: Path, allow_writes: bool = True, write_block_reason: str = ""):
        self.state_dir = Path(state_dir).resolve()
        self.file_path = self.state_dir / "security_state.json"
        self.allow_writes = allow_writes
        self.write_block_reason = (
            write_block_reason
            or "Penulisan security_state diblokir karena penyimpanan persisten tidak tersedia."
        )

    def load(self) -> Dict[str, Any]:
        if not self.allow_writes:
            return {}
        try:
            if self.file_path.exists():
                data = json.loads(self.file_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def save(self, owner_id: Optional[int], owner_label: str, web_access_key: str, webhook_secret: str) -> None:
        if not self.allow_writes:
            raise StorageUnavailableError(self.write_block_reason)
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
            raise

    @staticmethod
    def generate_webhook_secret() -> str:
        return secrets.token_urlsafe(32)


_FORBIDDEN_STORAGE_ROOTS = (
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/ms-playwright",
    "/proc",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
)

_EPHEMERAL_FSTYPES = {
    "autofs",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "mqueue",
    "overlay",
    "overlayfs",
    "proc",
    "pstore",
    "ramfs",
    "securityfs",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}


def is_railway_production_env(env: Optional[Mapping[str, str]] = None) -> bool:
    e = env if env is not None else os.environ
    return bool(
        (e.get("RAILWAY_ENVIRONMENT") or "").strip()
        or (e.get("RAILWAY_ENVIRONMENT_ID") or "").strip()
        or (e.get("RAILWAY_ENVIRONMENT_NAME") or "").strip()
        or (e.get("RAILWAY_PROJECT_ID") or "").strip()
        or (e.get("RAILWAY_SERVICE_ID") or "").strip()
        or (e.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
        or (e.get("APP_ENV") or "").strip().lower() == "production"
    )


def _decode_mountinfo_path(raw_path: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda m: chr(int(m.group(1), 8)),
        raw_path,
    )


def _is_forbidden_storage_path(candidate: Path, *, disallow_tmp: bool = False) -> Optional[str]:
    resolved = str(candidate.resolve())
    if resolved == "/":
        return "Path penyimpanan tidak boleh menunjuk ke root filesystem ('/')."
    for bad in _FORBIDDEN_STORAGE_ROOTS:
        if bad == "/":
            continue
        if resolved == bad or resolved.startswith(bad + "/"):
            return f"Path penyimpanan mengarah ke direktori sistem terlarang ('{bad}')."
    if disallow_tmp:
        for tmp_root in ("/tmp", "/var/tmp", "/app"):
            if resolved == tmp_root or resolved.startswith(tmp_root + "/"):
                return f"Path penyimpanan mengarah ke lokasi ephemeral ('{tmp_root}')."
    return None


def _is_within_directory(target: Path, parent: Path) -> bool:
    t_res = target.resolve()
    p_res = parent.resolve()
    return t_res == p_res or p_res in t_res.parents


def verify_persistent_volume_mount(
    env: Optional[Mapping[str, str]] = None,
    *,
    mountinfo_text: Optional[str] = None,
    ismount_fn: Optional[Callable[[str], bool]] = None,
) -> Tuple[bool, Optional[Path], str]:
    """
    Memverifikasi keberadaan Volume persisten secara ketat menggunakan:
    1) Environment variable resmi RAILWAY_VOLUME_MOUNT_PATH, DAN
    2) Bukti mount kernel (/proc/self/mountinfo dengan validasi fstype non-ephemeral & rw,
       atau os.path.ismount bila mountinfo tidak tersedia).
    Direktori biasa (mis. hasil mkdir /data) TIDAK PERNAH dianggap sebagai Volume persisten.
    """
    e = env if env is not None else os.environ
    raw_mount = (e.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if not raw_mount:
        return False, None, "RAILWAY_VOLUME_MOUNT_PATH belum terkonfigurasi (Volume persisten tidak terdeteksi)."

    mount_path = Path(raw_mount).resolve()
    bad_msg = _is_forbidden_storage_path(mount_path, disallow_tmp=True)
    if bad_msg:
        return False, None, f"RAILWAY_VOLUME_MOUNT_PATH tidak valid ({mount_path}): {bad_msg}"

    if not mount_path.exists() or not mount_path.is_dir():
        return False, None, f"Direktori mount Volume '{mount_path}' tidak ditemukan pada container."

    minfo: Optional[str] = mountinfo_text
    if minfo is None:
        proc_minfo = Path("/proc/self/mountinfo")
        if proc_minfo.exists():
            try:
                minfo = proc_minfo.read_text(encoding="utf-8")
            except OSError:
                minfo = None

    mount_str = str(mount_path)
    if minfo is not None:
        matched_entry: Optional[Tuple[str, str]] = None
        for line in minfo.splitlines():
            line = line.strip()
            if not line or " - " not in line:
                continue
            left, right = line.split(" - ", 1)
            left_parts = left.split()
            right_parts = right.split()
            if len(left_parts) < 6 or len(right_parts) < 1:
                continue
            mp = _decode_mountinfo_path(left_parts[4])
            mount_opts = left_parts[5]
            fstype = right_parts[0].lower()
            if mp == mount_str:
                matched_entry = (mount_opts, fstype)

        if matched_entry is None:
            return (
                False,
                None,
                f"Direktori '{mount_path}' ada di disk tetapi BUKAN mount point Volume persisten di kernel.",
            )

        mount_opts, fstype = matched_entry
        if fstype in _EPHEMERAL_FSTYPES:
            return (
                False,
                None,
                f"Mount pada '{mount_path}' bertipe '{fstype}' (ephemeral), bukan Volume persisten.",
            )
        opt_tokens = {tok.strip() for tok in mount_opts.split(",") if tok.strip()}
        if "ro" in opt_tokens:
            return False, None, f"Volume '{mount_path}' ter-mount dalam mode read-only (ro)."

        if ismount_fn is not None and not ismount_fn(mount_str):
            return False, None, f"Pemeriksaan ismount gagal pada '{mount_path}'."
        return True, mount_path, "OK"

    check_mount = ismount_fn if ismount_fn is not None else os.path.ismount
    if not check_mount(mount_str):
        return (
            False,
            None,
            f"Direktori '{mount_path}' bukan mount point aktif (os.path.ismount=False).",
        )
    return True, mount_path, "OK"


def _probe_directory_non_destructive(target_dir: Path) -> None:
    """
    Memastikan direktori tersedia (exist_ok=True, tanpa menyentuh file existing)
    dan melakukan uji tulis non-destruktif menggunakan nama file unik + O_CREAT | O_EXCL
    yang langsung dibersihkan kembali pada blok finally.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    probe_name = f".nusa_storage_probe_{os.getpid()}_{time.time_ns()}_{secrets.token_hex(12)}.tmp"
    probe_path = target_dir / probe_name
    fd: Optional[int] = None
    created = False
    try:
        fd = os.open(str(probe_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        payload = b"nusa_probe_ok"
        written = os.write(fd, payload)
        if written != len(payload):
            raise OSError("Write probe tidak berhasil menulis byte utuh.")
        os.fsync(fd)
        os.close(fd)
        fd = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if created:
            try:
                os.unlink(str(probe_path))
            except FileNotFoundError:
                pass


def resolve_storage_config(
    base_dir: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    mountinfo_text: Optional[str] = None,
    ismount_fn: Optional[Callable[[str], bool]] = None,
) -> Dict[str, Any]:
    """
    Menentukan dan memvalidasi path BROWSER_PROFILE_DIR & WORKSPACE_DIR:
    - Di Railway / Production: WAJIB berada di dalam Volume persisten terverifikasi (RAILWAY_VOLUME_MOUNT_PATH
      + mount kernel valid + lolos uji tulis). Jika gagal, TIDAK PERNAH dialihkan ke /tmp atau filesystem
      ephemeral; langsung mengembalikan ok=False & allow_writes=False (fail-closed).
    - Di lingkungan pengembangan / pengujian lokal: berjalan normal pada direktori lokal/sementara,
      namun tetap menetapkan storage_persistent=False kecuali bukti Volume persisten terpenuhi 100%.
    """
    e = env if env is not None else os.environ
    root_base = (base_dir or Path(__file__).resolve().parent.parent).resolve()
    is_prod = is_railway_production_env(e)

    vol_ok, vol_path, vol_msg = verify_persistent_volume_mount(
        e, mountinfo_text=mountinfo_text, ismount_fn=ismount_fn
    )

    explicit_profile = (e.get("BROWSER_PROFILE_DIR") or "").strip()
    explicit_workspace = (e.get("WORKSPACE_DIR") or "").strip()

    if is_prod:
        if not vol_ok or vol_path is None:
            unmounted_target = Path((e.get("RAILWAY_VOLUME_MOUNT_PATH") or "/data").strip() or "/data")
            return {
                "ok": False,
                "allow_writes": False,
                "storage_persistent": False,
                "storage_mode": "error",
                "is_production": True,
                "profile_dir": str(Path(explicit_profile) if explicit_profile else (unmounted_target / "nusa_operator_profile")),
                "workspace_dir": str(Path(explicit_workspace) if explicit_workspace else (unmounted_target / "workspace_files")),
                "volume_mount_path": str(vol_path) if vol_path else "",
                "error": f"Penyimpanan persisten Railway tidak tersedia atau gagal diverifikasi: {vol_msg}",
            }

        profile_path = Path(explicit_profile).resolve() if explicit_profile else (vol_path / "nusa_operator_profile").resolve()
        workspace_path = Path(explicit_workspace).resolve() if explicit_workspace else (vol_path / "workspace_files").resolve()

        for label, p in (("BROWSER_PROFILE_DIR", profile_path), ("WORKSPACE_DIR", workspace_path)):
            bad = _is_forbidden_storage_path(p, disallow_tmp=True)
            if bad:
                return {
                    "ok": False,
                    "allow_writes": False,
                    "storage_persistent": False,
                    "storage_mode": "error",
                    "is_production": True,
                    "profile_dir": str(profile_path),
                    "workspace_dir": str(workspace_path),
                    "volume_mount_path": str(vol_path),
                    "error": f"Konfigurasi {label} ditolak di production: {bad}",
                }
            if not _is_within_directory(p, vol_path):
                return {
                    "ok": False,
                    "allow_writes": False,
                    "storage_persistent": False,
                    "storage_mode": "error",
                    "is_production": True,
                    "profile_dir": str(profile_path),
                    "workspace_dir": str(workspace_path),
                    "volume_mount_path": str(vol_path),
                    "error": (
                        f"Konfigurasi {label} ('{p}') berada di luar Volume persisten '{vol_path}'. "
                        "Penulisan ke filesystem sementara diblokir."
                    ),
                }

        try:
            _probe_directory_non_destructive(profile_path)
            _probe_directory_non_destructive(workspace_path)
        except Exception as exc:
            return {
                "ok": False,
                "allow_writes": False,
                "storage_persistent": False,
                "storage_mode": "error",
                "is_production": True,
                "profile_dir": str(profile_path),
                "workspace_dir": str(workspace_path),
                "volume_mount_path": str(vol_path),
                "error": f"Volume persisten '{vol_path}' gagal diakses atau tidak writable: {exc}",
            }

        return {
            "ok": True,
            "allow_writes": True,
            "storage_persistent": True,
            "storage_mode": "railway_volume",
            "is_production": True,
            "profile_dir": str(profile_path),
            "workspace_dir": str(workspace_path),
            "volume_mount_path": str(vol_path),
            "error": "",
        }

    # -------------------- LINGKUNGAN LOKAL / PENGUJIAN --------------------
    if vol_ok and vol_path is not None:
        profile_path = Path(explicit_profile).resolve() if explicit_profile else (vol_path / "nusa_operator_profile").resolve()
        workspace_path = Path(explicit_workspace).resolve() if explicit_workspace else (vol_path / "workspace_files").resolve()
    else:
        profile_path = Path(explicit_profile).resolve() if explicit_profile else Path("/tmp/nusa_operator_profile").resolve()
        workspace_path = Path(explicit_workspace).resolve() if explicit_workspace else (root_base / "workspace_files").resolve()

    for label, p in (("BROWSER_PROFILE_DIR", profile_path), ("WORKSPACE_DIR", workspace_path)):
        bad = _is_forbidden_storage_path(p, disallow_tmp=False)
        if bad:
            return {
                "ok": False,
                "allow_writes": False,
                "storage_persistent": False,
                "storage_mode": "error",
                "is_production": False,
                "profile_dir": str(profile_path),
                "workspace_dir": str(workspace_path),
                "volume_mount_path": str(vol_path) if vol_path else "",
                "error": f"Konfigurasi {label} tidak valid: {bad}",
            }

    try:
        _probe_directory_non_destructive(profile_path)
        _probe_directory_non_destructive(workspace_path)
    except Exception as exc:
        return {
            "ok": False,
            "allow_writes": False,
            "storage_persistent": False,
            "storage_mode": "error",
            "is_production": False,
            "profile_dir": str(profile_path),
            "workspace_dir": str(workspace_path),
            "volume_mount_path": str(vol_path) if vol_path else "",
            "error": f"Direktori penyimpanan lokal tidak writable: {exc}",
        }

    verified_on_volume = bool(
        vol_ok
        and vol_path is not None
        and _is_within_directory(profile_path, vol_path)
        and _is_within_directory(workspace_path, vol_path)
    )
    return {
        "ok": True,
        "allow_writes": True,
        "storage_persistent": verified_on_volume,
        "storage_mode": "railway_volume" if verified_on_volume else "local_ephemeral",
        "is_production": False,
        "profile_dir": str(profile_path),
        "workspace_dir": str(workspace_path),
        "volume_mount_path": str(vol_path) if verified_on_volume and vol_path else "",
        "error": "",
    }
