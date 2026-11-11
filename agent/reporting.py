from typing import Any, Dict, List, Optional

from agent.security import redact_secrets
from agent.task_store import VALID_FINAL_STATUSES, summarize_task_name


def compute_step_metrics(
    history: List[Dict[str, Any]],
    plan: List[Dict[str, Any]],
    browser_actions_count: int,
    final_status: str,
) -> Dict[str, int]:
    """
    Menghitung metrik langkah secara faktual & konsisten:
    - TIDAK PERNAH menghasilkan 0 langkah apabila browser benar-benar melakukan aksi!
    - Memisahkan langkah internal 'verify_gate' / 'auto_cred_hint' dari langkah kerja nyata.
    """
    internal_gates = {"verify_gate", "auto_cred_hint"}
    real_entries = [h for h in (history or []) if h.get("action_name") not in internal_gates]

    succeeded = 0
    failed = 0
    for h in real_entries:
        res = str(h.get("result") or "").strip()
        if (
            res.startswith("❌")
            or res.startswith("⛔")
            or "[UNREACHABLE_SITE]" in res
            or "memblokir akses" in res.lower()
        ):
            failed += 1
        else:
            succeeded += 1

    # Jaminan keras: jika browser terbukti mengeksekusi aksi, total langkah nyata minimal sebesar browser_actions_count!
    if browser_actions_count > 0 and (succeeded + failed) < browser_actions_count:
        if final_status == "GAGAL" and succeeded == 0:
            failed = max(failed, browser_actions_count)
        else:
            succeeded = max(succeeded, browser_actions_count - failed)

    raw_plan_len = len(plan or [])
    uncompleted_plan = sum(
        1 for p in (plan or []) if (p.get("status") or "").lower() not in ("completed", "done")
    )

    norm_status = final_status.upper().strip()
    if norm_status == "BERHASIL":
        pending = 0
        planned = max(raw_plan_len, succeeded + failed, 1 if (succeeded + failed) > 0 else raw_plan_len)
    elif norm_status in ("MENUNGGU PERSETUJUAN", "TERHENTI", "SEBAGIAN BERHASIL"):
        pending = max(1, uncompleted_plan)
        planned = max(raw_plan_len, succeeded + failed + pending)
    else:  # GAGAL
        pending = max(1 if succeeded == 0 and failed == 0 else uncompleted_plan, 0)
        planned = max(raw_plan_len, succeeded + failed + pending, 1)

    return {
        "planned": planned,
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
        "total_executed": succeeded + failed,
    }


def format_execution_report(
    instruction: str,
    final_status: str,
    metrics: Dict[str, int],
    duration_sec: int,
    issues: Optional[List[str]] = None,
    verification_summary: str = "",
    narrative_summary: str = "",
    has_screenshot: bool = False,
) -> str:
    """
    MODUL 5 — EXECUTION REPORTING ENGINE:
    Menyusun laporan eksekusi standar ke Telegram yang memuat ke-10 komponen wajib secara akurat,
    jujur, dan bebas kredensial mentah.
    """
    norm_status = final_status.upper().strip()
    if norm_status not in VALID_FINAL_STATUSES:
        norm_status = "GAGAL"

    status_badge = {
        "BERHASIL": "✅ BERHASIL",
        "GAGAL": "❌ GAGAL",
        "SEBAGIAN BERHASIL": "⚠️ SEBAGIAN BERHASIL",
        "MENUNGGU PERSETUJUAN": "🛡️ MENUNGGU PERSETUJUAN",
        "TERHENTI": "⏹️ TERHENTI",
    }[norm_status]

    job_title = summarize_task_name(instruction)
    dur_val = max(1, int(duration_sec or 1))

    clean_issues = [redact_secrets(i).strip() for i in (issues or []) if str(i).strip()]
    if clean_issues:
        obstacles_txt = "; ".join(clean_issues[:5])
    else:
        obstacles_txt = "Tidak ada kendala" if norm_status == "BERHASIL" else "Lihat rincian verifikasi di bawah"

    ver_txt = redact_secrets(verification_summary or "").strip()
    if not ver_txt:
        if norm_status == "BERHASIL":
            ver_txt = "Terverifikasi sesuai instruksi"
        elif norm_status == "MENUNGGU PERSETUJUAN":
            ver_txt = "Menunggu konfirmasi pengguna sebelum mengeksekusi aksi sensitif"
        elif norm_status == "SEBAGIAN BERHASIL":
            ver_txt = "Sebagian tahapan terverifikasi, namun masih ada langkah tersisa"
        elif norm_status == "TERHENTI":
            ver_txt = "Eksekusi dihentikan sebelum seluruh langkah terverifikasi"
        else:
            ver_txt = "Verifikasi akhir menunjukkan target belum terpenuhi"

    screenshot_info = "Terlampir pada pesan foto" if has_screenshot else "Tidak tersedia (tugas non-visual / halaman belum terbuka)"

    report_block = (
        "📋 LAPORAN EKSEKUSI PEKERJAAN\n"
        f"• Nama Pekerjaan: {job_title}\n"
        f"• Status Akhir: {status_badge}\n"
        f"• Langkah Direncanakan: {metrics.get('planned', 0)} langkah\n"
        f"• Langkah Berhasil: {metrics.get('succeeded', 0)} langkah\n"
        f"• Langkah Gagal: {metrics.get('failed', 0)} langkah\n"
        f"• Langkah Belum Selesai: {metrics.get('pending', 0)} langkah\n"
        f"• Durasi Eksekusi: {dur_val} detik\n"
        f"• Kendala Ditemukan: {obstacles_txt}\n"
        f"• Hasil Verifikasi Akhir: {ver_txt}\n"
        f"• Bukti Screenshot: {screenshot_info}"
    )

    clean_narrative = redact_secrets(narrative_summary or "").strip()
    if clean_narrative:
        return f"{clean_narrative}\n\n{report_block}"
    return report_block
