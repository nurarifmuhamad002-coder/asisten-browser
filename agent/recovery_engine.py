import asyncio
from typing import Any, Dict, List, Optional, Tuple

from agent.security import redact_secrets


class ErrorRecoveryEngine:
    """
    MODUL 3 — ERROR RECOVERY ENGINE:
    1. Mendeteksi error secara terstruktur:
       - browser_timeout
       - element_not_found
       - page_load_failed
       - website_error_message
       - blind_repeat_action (circuit breaker anti klik berulang membabi buta!)
       - browser_crash
    2. Menganalisis penyebab kegagalan secara spesifik.
    3. Mencoba strategi alternatif yang aman dengan batas percobaan (bounded retry <= 2).
    4. Memulihkan sesi browser ke URL terverifikasi terakhir bila tab crash/hang.
    5. Melaporkan penyebab spesifik bila tetap gagal — DILARANG mengklaim berhasil tanpa verifikasi!
    """

    MAX_RECOVERY_ATTEMPTS_PER_STEP = 2
    MAX_IDENTICAL_NO_DELTA_CLICKS = 1  # Klik ke-2 di target yang sama tanpa perubahan DOM langsung dicegah!

    def __init__(self) -> None:
        self.reset_run()

    def reset_run(self) -> None:
        self._action_streak: List[Tuple[str, str]] = []  # (action_sig, dom_signature_before)
        self._step_retry_counts: Dict[str, int] = {}
        self.recovery_events: List[Dict[str, str]] = []

    @staticmethod
    def build_action_signature(action: Dict[str, Any], current_url: str = "") -> str:
        name = (action.get("name") or "").lower()
        u = (current_url or "").split("#", 1)[0]
        if name == "click":
            return f"click|{u}|idx={action.get('index')}"
        if name == "type":
            return f"type|{u}|idx={action.get('index')}|len={len(str(action.get('text') or ''))}|enter={bool(action.get('press_enter'))}"
        if name == "select":
            return f"select|{u}|idx={action.get('index')}|opt={action.get('option')}"
        if name == "navigate":
            return f"nav|{action.get('url')}"
        return f"{name}|{u}"

    def check_blind_repeat_before_action(
        self,
        action: Dict[str, Any],
        dom_state: Dict[str, Any],
    ) -> Optional[str]:
        """
        Mencegah agen mengklik tombol yang sama berulang-ulang secara membabi buta ketika aksi sebelumnya
        terbukti tidak mengubah URL maupun DOM halaman sama sekali.
        """
        act_name = (action.get("name") or "").lower()
        if act_name not in ("click", "select"):
            return None

        cur_url = dom_state.get("url", "")
        cur_dom_sig = dom_state.get("dom_signature", "")
        act_sig = self.build_action_signature(action, cur_url)

        same_no_delta = sum(
            1
            for prev_act_sig, prev_dom_sig in self._action_streak[-3:]
            if prev_act_sig == act_sig and prev_dom_sig == cur_dom_sig
        )
        if same_no_delta >= self.MAX_IDENTICAL_NO_DELTA_CLICKS:
            cause = (
                f"Aksi '{act_name}' pada elemen [{action.get('index')}] sudah dicoba tetapi halaman/DOM tidak berubah sama sekali"
            )
            page_errs = dom_state.get("page_errors") or []
            if page_errs:
                cause += f" (Pesan error halaman: {'; '.join(page_errs[:2])})"
            self.recovery_events.append(
                {
                    "error_type": "blind_repeat_action",
                    "cause": redact_secrets(cause),
                    "strategy": "Memblokir klik ulang buta & mengarahkan evaluasi alternatif selector/validasi form.",
                    "outcome": "blocked_blind_repeat",
                }
            )
            return (
                f"⛔ [RECOVERY CIRCUIT BREAKER] DILARANG mengulang klik pada elemen [{action.get('index')}] secara membabi buta! "
                f"{cause}. Periksa apakah ada field wajib yang belum valid, perlu scroll, atau gunakan elemen/strategi lain."
            )
        return None

    def record_action_delta(
        self,
        action: Dict[str, Any],
        before_dom: Dict[str, Any],
    ) -> None:
        act_sig = self.build_action_signature(action, before_dom.get("url", ""))
        self._action_streak.append((act_sig, before_dom.get("dom_signature", "")))
        if len(self._action_streak) > 12:
            self._action_streak = self._action_streak[-12:]

    @staticmethod
    def classify_error(result_text: str, dom_state: Optional[Dict[str, Any]] = None) -> Optional[Tuple[str, str]]:
        """
        Mengklasifikasikan jenis error & merumuskan akar penyebabnya secara jujur.
        Mengembalikan (error_type, specific_cause) atau None bila aksi berhasil normal.
        """
        txt = (result_text or "").strip()
        low = txt.lower()

        if "[unreachable_site]" in low or "memblokir akses" in low or "403/anti-bot" in low:
            return ("page_load_failed", redact_secrets(txt))

        if "target crashed" in low or "target closed" in low or "browser belum aktif" in low:
            return ("browser_crash", redact_secrets(txt))

        if "timeout" in low or "timed out" in low or "tidak merespons" in low:
            return ("browser_timeout", redact_secrets(txt))

        if txt.startswith("❌"):
            if "tidak ditemukan" in low or "not found" in low:
                return ("element_not_found", redact_secrets(txt.lstrip("❌ ").strip()))
            if "submit gagal" in low or "tahan submit" in low or "verifikasi pasca-submit gagal" in low:
                return ("website_error_message", redact_secrets(txt.lstrip("❌ ").strip()))
            return ("action_execution_error", redact_secrets(txt.lstrip("❌ ").strip()))

        if dom_state:
            page_errs = dom_state.get("page_errors") or []
            if page_errs and any("http/halaman error" in pe.lower() for pe in page_errs):
                return ("page_load_failed", redact_secrets("; ".join(page_errs[:2])))

        return None

    async def attempt_browser_recovery(
        self,
        browser: Any,
        action: Dict[str, Any],
        error_type: str,
        cause: str,
        last_verified_url: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Menjalankan pemulihan otomatis yang AMAN dengan batas maksimal 2 kali per target:
        - element_not_found -> re-inspect DOM segar, scroll bertahap ke bawah/atas, lalu cari ulang selector stabil/label.
        - browser_timeout -> hentikan resource iklan yang menggantung (jika body sudah ter-render) & tunggu stabil.
        - browser_crash -> bangkitkan ulang browser & pulihkan ke last_verified_url agar lanjut dari langkah terverifikasi.
        Mengembalikan dict {"recovered": bool, "new_result": str, "strategy": str} atau None jika tidak dapat di-retry otomatis.
        """
        act_name = (action.get("name") or "").lower()
        retry_key = f"{act_name}:{action.get('index')}:{action.get('url', '')}"
        used = self._step_retry_counts.get(retry_key, 0)
        if used >= self.MAX_RECOVERY_ATTEMPTS_PER_STEP:
            return None
        self._step_retry_counts[retry_key] = used + 1

        # 1. Pemulihan Crash Browser -> Hidupkan ulang & kembali ke URL terverifikasi terakhir
        if error_type == "browser_crash":
            strategy = "Menghidupkan ulang sesi Chromium & memulihkan halaman terakhir yang terverifikasi"
            try:
                await browser.ensure_alive()
                if last_verified_url and not last_verified_url.startswith(("about:", "chrome-error://")):
                    await browser.navigate(last_verified_url)
                self.recovery_events.append(
                    {
                        "error_type": error_type,
                        "cause": cause,
                        "strategy": strategy,
                        "outcome": "browser_restored",
                    }
                )
                return {
                    "recovered": True,
                    "strategy": strategy,
                    "new_result": (
                        f"♻️ [AUTO-RECOVERY] Sesi browser berhasil dipulihkan kembali ke '{last_verified_url or 'halaman aktif'}'. "
                        "Silakan lanjutkan dari langkah terverifikasi terakhir."
                    ),
                }
            except Exception as exc:
                self.recovery_events.append(
                    {
                        "error_type": error_type,
                        "cause": cause,
                        "strategy": strategy,
                        "outcome": f"failed: {redact_secrets(str(exc))[:120]}",
                    }
                )
                return None

        # 2. Pemulihan Elemen Tidak Ditemukan -> Scroll + Tutup Pop-up Penghalang + Re-Inspect DOM Segar
        if error_type == "element_not_found" and act_name in ("click", "type", "select"):
            idx = int(action.get("index", 0))
            direction = "down" if used == 0 else "top"
            strategy = f"Menutup pop-up penghalang, scroll '{direction}', membaca ulang DOM segar, dan mencoba ulang aksi '{act_name}'"
            try:
                await browser.dismiss_obstructions()
                await browser.scroll_page(direction=direction, amount=460)
                await asyncio.sleep(0.25)
                fresh_dom = await browser.observe(annotated_screenshot=False)
                fresh_els = fresh_dom.get("elements", [])

                if act_name == "click":
                    retry_res = await browser.click_element(idx, fresh_els)
                elif act_name == "type":
                    retry_res = await browser.type_into_element(
                        index=idx,
                        text=str(action.get("text", "")),
                        elements=fresh_els,
                        press_enter=bool(action.get("press_enter", False)),
                    )
                else:
                    retry_res = await browser.select_dropdown(
                        index=idx,
                        option_value_or_text=str(action.get("option", "")),
                        elements=fresh_els,
                    )

                ok = not retry_res.startswith("❌")
                self.recovery_events.append(
                    {
                        "error_type": error_type,
                        "cause": cause,
                        "strategy": strategy,
                        "outcome": "recovered" if ok else f"still_failed: {redact_secrets(retry_res)[:120]}",
                    }
                )
                if ok:
                    return {
                        "recovered": True,
                        "strategy": strategy,
                        "new_result": f"♻️ [PULIH OTOMATIS] {retry_res}",
                    }
            except Exception as exc:
                self.recovery_events.append(
                    {
                        "error_type": error_type,
                        "cause": cause,
                        "strategy": strategy,
                        "outcome": f"exception: {redact_secrets(str(exc))[:120]}",
                    }
                )
            return None

        # 3. Pemulihan Browser Timeout saat Aksi/Navigasi -> Stabilkan Halaman & Coba Sekali Lagi
        if error_type == "browser_timeout":
            strategy = "Menghentikan sub-resource lambat yang menggantung & menstabilkan DOM"
            try:
                await browser.wait_for_page_stable(timeout_ms=3000)
                if act_name == "navigate" and action.get("url") and used == 0:
                    retry_nav = await browser.navigate(str(action.get("url")))
                    ok = "[UNREACHABLE_SITE]" not in retry_nav and not retry_nav.startswith("❌")
                    self.recovery_events.append(
                        {
                            "error_type": error_type,
                            "cause": cause,
                            "strategy": strategy + " lalu memuat ulang URL",
                            "outcome": "recovered" if ok else redact_secrets(retry_nav)[:120],
                        }
                    )
                    if ok:
                        return {
                            "recovered": True,
                            "strategy": strategy,
                            "new_result": f"♻️ [PULIH OTOMATIS] {retry_nav}",
                        }
            except Exception:
                pass
        return None
