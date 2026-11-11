import re
from typing import Any, Dict, Optional

SENSITIVE_PATTERNS = [
    (
        "Transaksi & Pembayaran",
        re.compile(
            r"\b(bayar|checkout|beli\s+sekarang|konfirmasi\s+pembayaran|konfirmasi\s+pesanan|"
            r"proses\s+pembayaran|pay\s+now|place\s+order|complete\s+purchase|transfer|"
            r"kirim\s+dana|kirim\s+uang|tarik\s+saldo|withdraw|subscribe|berlangganan)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Penghapusan / Aksi Destruktif",
        re.compile(
            r"\b(hapus\s+akun|hapus\s+data|hapus\s+permanen|delete\s+account|delete\s+permanently|"
            r"remove\s+account|tutup\s+akun|reset\s+akun|revoke|cabut\s+akses)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Keamanan Akun & Kredensial",
        re.compile(
            r"\b(ubah\s+password|ganti\s+password|change\s+password|update\s+security|"
            r"matikan\s+2fa|disable\s+2fa|authorize\s+app|izinkan\s+akses)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Pengiriman Pesan / Publikasi Eksternal",
        re.compile(
            r"\b(kirim\s+email|send\s+email|send\s+message|publish\s+now|posting\s+sekarang|"
            r"ajukan\s+pinjaman|submit\s+application)\b",
            re.IGNORECASE,
        ),
    ),
]


def inspect_element_sensitivity(element: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Mengecek secara deterministik apakah elemen yang hendak diklik oleh Agent
    termasuk tindakan sensitif yang WAJIB meminta persetujuan user terlebih dahulu.
    """
    if not element:
        return None

    tag = (element.get("tag") or "").lower()
    etype = (element.get("type") or "").lower()
    # Hanya intercept tombol/link/submit, bukan sekadar klik kolom teks biasa
    is_clickable = tag in ("button", "a", "summary") or etype in ("submit", "button") or element.get("data_sensitive")
    if not is_clickable:
        return None

    combined_label = " ".join(
        filter(
            None,
            [
                element.get("text", ""),
                element.get("label", ""),
                element.get("aria_label", ""),
            ],
        )
    ).strip()

    if element.get("data_sensitive"):
        return {
            "category": "Tindakan Sensitif Terproteksi",
            "target_label": combined_label or f"Elemen [{element.get('index')}]",
            "reason": "Tombol ini ditandai sebagai aksi sensitif pada halaman dan memerlukan persetujuan langsung dari pemilik akun.",
            "rect": element.get("rect"),
        }

    # Jangan memblokir navigasi tab biasa yang hanya membuka halaman form
    if etype == "tab" or "tabbtn" in (element.get("el_id") or "").lower():
        return None

    for category, pattern in SENSITIVE_PATTERNS:
        if pattern.search(combined_label):
            return {
                "category": category,
                "target_label": combined_label,
                "reason": f"Terdeteksi kata kunci aksi sensitif ({category}) pada tombol '{combined_label}'.",
                "rect": element.get("rect"),
            }

    return None


PRACTICE_SANDBOX_HOSTS = (
    "testpages.eviltester.com",
    "testpages.herokuapp.com",
    "demoqa.com",
    "the-internet.herokuapp.com",
    "automationexercise.com",
    "practicetestautomation.com",
    "saucedemo.com",
)


def is_practice_sandbox_url(url: str) -> bool:
    u = (url or "").lower()
    return any(host in u for host in PRACTICE_SANDBOX_HOSTS)


def is_credential_element(element: Optional[Dict[str, Any]]) -> bool:
    """
    Mencegah Agent mengetik langsung ke kolom password atau kode OTP/2FA.
    Login dan autentikasi harus selalu dilakukan langsung oleh user via Live Takeover.
    """
    if not element:
        return False
    if element.get("is_password") or (element.get("type") or "").lower() == "password":
        return True
    combined = " ".join(
        [
            element.get("name", ""),
            element.get("el_id", ""),
            element.get("placeholder", ""),
            element.get("label", ""),
            element.get("aria_label", ""),
        ]
    )
    return bool(re.search(r"\b(password|kata\s*sandi|passcode|otp|2fa|kode\s*verifikasi|pin)\b", combined, re.IGNORECASE))


def inspect_non_browser_sensitivity(action: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Memeriksa apakah aksi non-browser (penghapusan file atau eksekusi skrip destruktif)
    memerlukan persetujuan eksplisit pemilik (Approval Gate) atau wajib diblokir.
    """
    from agent.security import inspect_script_safety

    act_name = (action.get("name") or "").lower()
    if act_name in ("delete_file", "remove_file", "rmdir", "delete_dir"):
        target_f = str(action.get("filename") or action.get("path") or "(file)")
        return {
            "decision": "approve",
            "category": "Penghapusan File / Direktori",
            "target_label": f"Hapus '{target_f}'",
            "reason": f"Agent meminta menghapus file/folder '{target_f}' dari penyimpanan workspace.",
        }

    if act_name == "run_python":
        code = str(action.get("code") or action.get("script") or "")
        chk = inspect_script_safety(code, lang="python")
        if chk.get("decision") in ("block", "approve"):
            return chk

    if act_name == "run_js":
        code = str(action.get("code") or action.get("script") or "")
        chk = inspect_script_safety(code, lang="js")
        if chk.get("decision") in ("block", "approve"):
            return chk

    return None
