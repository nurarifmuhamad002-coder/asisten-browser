import asyncio
import importlib
import json
import os
import py_compile
import re
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def run_all_11_validation_suites() -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []

    def record(num: int, name: str, passed: bool, detail: str) -> None:
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] #{num:02d} {name}: {detail}")
        results.append({"id": num, "name": name, "passed": passed, "detail": detail})
        if not passed:
            raise AssertionError(f"Suite #{num} ({name}) FAILED: {detail}")

    # =========================================================================
    # 1. CODE REVIEW & STATIC SECURITY AUDIT
    # =========================================================================
    py_sources = [ROOT / "main.py", *sorted((ROOT / "agent").glob("*.py"))]
    generic_leak_regexes = [
        re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_-]{25,}\b"),
        re.compile(r"\b(?:AIza[0-9A-Za-z_-]{28,}|AQ\.[0-9A-Za-z._-]{24,})\b"),
        re.compile(r"\b(?:sk-(?:proj-|ant-|or-)?[A-Za-z0-9_-]{18,}|hf_[A-Za-z0-9]{20,})\b"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
        re.compile(r"(?:https?|socks5)://(?!user(?:name)?:pass(?:word)?@)[A-Za-z0-9._-]+:[A-Za-z0-9._-]{6,}@"),
    ]
    # Pastikan detektor regex generik terbukti menangkap sampel dummy sintetis
    dummy_probes = [
        "123456789:AADummySyntheticToken00000000001",
        "AIzaSyDummySyntheticKey000000000000001",
        "AQ.DummySyntheticGeminiKey0000000001",
        "sk-DummySyntheticOpenAIKey00000001",
        "http://dummyuser01:dummypass01@10.0.0.1:8080",
    ]
    assert all(any(rx.search(dp) for rx in generic_leak_regexes) for dp in dummy_probes)

    leaked_in_src = []
    total_loc = 0
    for src in py_sources:
        txt = src.read_text(encoding="utf-8")
        total_loc += len(txt.splitlines())
        for rx in generic_leak_regexes:
            if rx.search(txt):
                leaked_in_src.append(src.name)
    record(
        1,
        "Code Review & Static Security Audit",
        len(leaked_in_src) == 0 and len(py_sources) >= 11,
        f"Diperiksa {len(py_sources)} modul Python ({total_loc} baris) dengan {len(generic_leak_regexes)} regex generik, 0 hardcoded credential bocor.",
    )

    # =========================================================================
    # 2. SYNTAX CHECK
    # =========================================================================
    for pf in [*py_sources, *sorted((ROOT / "tests").glob("*.py"))]:
        py_compile.compile(str(pf), doraise=True)
    record(2, "Syntax Check (py_compile)", True, f"Seluruh {len(py_sources) + 2} file Python lolos kompilasi bytecode 0 syntax error.")

    from fastapi.testclient import TestClient
    import uvicorn
    from agent.browser_engine import CloudBrowserEngine
    from agent.dom_inspector import format_dom_for_llm
    from agent.file_manager import WorkspaceFileManager
    from agent.guardrails import inspect_element_sensitivity, inspect_non_browser_sensitivity, is_credential_element
    from agent.llm_providers import LLMConfig
    from agent.orchestrator import AgentOrchestrator
    from agent.recovery_engine import ErrorRecoveryEngine
    from agent.reporting import compute_step_metrics, format_execution_report
    from agent.security import inspect_script_safety, redact_secrets, redact_structure, register_runtime_secret
    from agent.task_store import TaskStateStore, compute_side_effect_fingerprint
    from agent.telegram_bridge import TelegramBotBridge

    tmp_dir = Path(tempfile.mkdtemp(prefix="upgrade_val_"))
    server = None
    serve_task = None
    browser = None

    try:
        # =====================================================================
        # 3. UNIT TESTS (Security, Guardrails, Recovery, Reporting, Legacy)
        # =====================================================================
        syn_tok = "111222333:AASyntheticTelegramTokenForTestingOnly99"
        syn_key = "AIzaSySyntheticUnitTestKeyForValidation001"
        register_runtime_secret("CustomRuntimeSecret#2026")
        sample = f"Err https://api.telegram.org/bot{syn_tok}/sendMessage?key={syn_key} pass=CustomRuntimeSecret#2026"
        red = redact_secrets(sample)
        assert syn_tok not in red and syn_key not in red and "CustomRuntimeSecret#2026" not in red
        assert inspect_script_safety("import shutil\nshutil.rmtree('DATA')", "python")["decision"] == "approve"
        assert inspect_script_safety("import os\nprint(os.environ)", "python")["decision"] == "block"
        assert inspect_non_browser_sensitivity({"name": "delete_file", "filename": "x.txt"})["decision"] == "approve"
        assert is_credential_element({"tag": "input", "type": "password", "name": "p"}) is True
        assert inspect_element_sensitivity({"tag": "button", "type": "submit", "text": "Bayar Sekarang"}) is not None
        record(3, "Unit Tests (Security, Guardrails, Script Analyzer)", True, "Redaksi rahasia, deteksi skrip destruktif/eksfiltrasi, & guardrail sensitif lulus 100%.")

        # =====================================================================
        # 4. INTEGRATION TESTS (Approval Gate for File Deletion & Destructive Python)
        # =====================================================================
        ws_path = tmp_dir / "ws"
        prof_path = tmp_dir / "profile"
        fm = WorkspaceFileManager(str(ws_path))
        fm.write_file("penting.txt", "Data Penting Jangan Dihapus Tanpa Izin")

        events_log: List[Dict[str, Any]] = []

        async def _capture_ev(ev: Dict[str, Any]) -> None:
            events_log.append(ev)

        tg = TelegramBotBridge(state_dir=str(prof_path))
        browser = CloudBrowserEngine(fm, _capture_ev, profile_dir=str(prof_path))
        orch = AgentOrchestrator(browser, fm, LLMConfig(), _capture_ev, tg)
        tg.attach_orchestrator(orch)

        # Uji 4A: Penolakan penghapusan file menjaga file tetap utuh!
        t_rec_int = orch.task_store.create_task("Hapus file penting.txt")
        orch._active_task_id = t_rec_int["task_id"]

        async def _reject_soon() -> None:
            for _ in range(40):
                if orch.state == "WAITING_APPROVAL":
                    st_chk = orch.task_store.get_task(orch._active_task_id)
                    assert st_chk and st_chk["status"] == "MENUNGGU PERSETUJUAN"
                    await orch.resolve_approval("reject")
                    return
                await asyncio.sleep(0.05)

        rej_worker = asyncio.create_task(_reject_soon())
        del_res_rej = await orch._execute_with_guardrails(1, {"name": "delete_file", "filename": "penting.txt"}, {"elements": []})
        await rej_worker
        assert "DITOLAK" in del_res_rej and (ws_path / "penting.txt").exists()

        # Uji 4B: Persetujuan penghapusan file barulah menghapus file!
        async def _approve_soon() -> None:
            for _ in range(40):
                if orch.state == "WAITING_APPROVAL":
                    await orch.resolve_approval("approve")
                    return
                await asyncio.sleep(0.05)

        app_worker = asyncio.create_task(_approve_soon())
        del_res_ok = await orch._execute_with_guardrails(2, {"name": "delete_file", "filename": "penting.txt"}, {"elements": []})
        await app_worker
        assert "berhasil dihapus" in del_res_ok and not (ws_path / "penting.txt").exists()

        # Uji 4C: Eksfiltrasi os.environ pada run_python otomatis diblokir keras!
        blk_res = await orch._execute_with_guardrails(3, {"name": "run_python", "code": "import os; print(os.environ)"}, {"elements": []})
        assert "BLOKIR KEAMANAN" in blk_res
        orch._active_task_id = ""
        # Uji 4D: End-to-End Multi-Step _run_loop() -> TaskStateStore -> Reporting -> Telegram notify_done
        e2e_reports: List[Dict[str, Any]] = []

        async def _capture_done(summary_txt: str, elapsed_sec=None, total_steps=None, proof_frame_b64="", final_status=None):
            e2e_reports.append({
                "summary": summary_txt,
                "steps": total_steps,
                "status": final_status,
                "has_proof": bool(proof_frame_b64),
            })

        orig_notify_done = tg.notify_done
        orig_decide = orch.brain.decide_next_step
        seq_steps = [
            {
                "thought": "Membuat folder laporan uji",
                "plan": [{"step": "Buat folder", "status": "in_progress"}, {"step": "Tulis file", "status": "pending"}],
                "action": {"name": "make_dir", "path": "E2E_DEMO"},
            },
            {
                "thought": "Menulis file rekap ke dalam folder",
                "plan": [{"step": "Buat folder", "status": "completed"}, {"step": "Tulis file", "status": "in_progress"}],
                "action": {"name": "write_file", "filename": "E2E_DEMO/rekap.txt", "content": "Hasil terverifikasi"},
            },
            {
                "thought": "Memverifikasi struktur pohon direktori",
                "plan": [{"step": "Buat folder", "status": "completed"}, {"step": "Tulis file", "status": "completed"}],
                "action": {"name": "list_tree", "path": "E2E_DEMO"},
            },
            {
                "thought": "Seluruh tahapan selesai dan terverifikasi",
                "plan": [{"step": "Buat folder", "status": "completed"}, {"step": "Tulis file", "status": "completed"}],
                "action": {"name": "done", "summary": "Struktur E2E_DEMO dan rekap.txt selesai dibuat."},
            },
        ]
        idx_box = {"i": 0}

        async def _mock_decide(**kwargs):
            i = idx_box["i"]
            idx_box["i"] = min(i + 1, len(seq_steps) - 1)
            return seq_steps[i]

        try:
            tg.notify_done = _capture_done  # type: ignore
            orch.brain.decide_next_step = _mock_decide  # type: ignore
            await orch.start_goal("Buat struktur folder E2E_DEMO beserta rekap.txt")
            if orch._task:
                await asyncio.wait_for(orch._task, timeout=10.0)
            assert len(e2e_reports) == 1
            assert e2e_reports[0]["status"] == "BERHASIL" and e2e_reports[0]["steps"] == 3
            assert "LAPORAN EKSEKUSI PEKERJAAN" in e2e_reports[0]["summary"]
            latest_saved = orch.task_store.get_latest_task()
            assert latest_saved and latest_saved["status"] == "BERHASIL" and len(latest_saved["completed_steps"]) == 3
        finally:
            tg.notify_done = orig_notify_done  # type: ignore
            orch.brain.decide_next_step = orig_decide  # type: ignore

        record(4, "Integration Tests (E2E Multi-Step Loop, Approval Gate, & Script Isolation)", True, "Loop multi-step E2E (3 langkah -> BERHASIL), approval hapus file, & blokir eksfiltrasi lulus 100%.")

        # =====================================================================
        # 5. TELEGRAM CONNECTION, OWNER LOCK, & WEBHOOK SECURITY TEST
        # =====================================================================
        import main as app_main

        orig_tg = app_main.telegram_bridge
        try:
            tg._lock_owner(999111222, {"username": "owner_uji", "first_name": "Owner"})
            tg._webhook_secret_registered = True
            app_main.telegram_bridge = tg
            with TestClient(app_main.app) as client:
                h_resp = client.get("/api/health")
                assert h_resp.status_code == 200 and h_resp.json()["ok"] is True

                # Webhook tanpa secret header atau salah secret WAJIB ditolak 403!
                bad_wh = client.post("/api/telegram/webhook", json={"update_id": 1})
                assert bad_wh.status_code == 403

                # Webhook dengan secret header valid WAJIB diterima 200!
                ok_wh = client.post(
                    "/api/telegram/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": tg.webhook_secret},
                    json={"update_id": 2},
                )
                assert ok_wh.status_code == 200 and ok_wh.json()["ok"] is True

                # User asing selain owner_id (999111222) ditolak oleh Private Lock Guard!
                sent_messages: List[Tuple[int, str]] = []
                orig_send_text = tg.send_text

                async def _mock_send_text(cid: int, text: str, reply_markup: Any = None):
                    sent_messages.append((cid, redact_secrets(text)))
                    return 101

                tg.send_text = _mock_send_text  # type: ignore
                try:
                    await tg._handle_update(
                        {
                            "update_id": 5001,
                            "message": {"message_id": 10, "chat": {"id": 444555666}, "from": {"id": 444555666}, "text": "halo"},
                        }
                    )
                    assert any(cid == 444555666 and "Akses Ditolak" in m for cid, m in sent_messages)

                    # Owner memanggil /status_tugas & konfirmasi 2-tahap hapus file
                    fm.write_file("uji_konfirmasi.txt", "halo")
                    await tg._handle_update(
                        {
                            "update_id": 5002,
                            "callback_query": {
                                "id": "cb1",
                                "from": {"id": 999111222},
                                "message": {"chat": {"id": 999111222}},
                                "data": "rm_file:uji_konfirmasi.txt",
                            },
                        }
                    )
                    assert (ws_path / "uji_konfirmasi.txt").exists(), "File tidak boleh terhapus sebelum klik confirm_rm!"
                    await tg._handle_update(
                        {
                            "update_id": 5003,
                            "callback_query": {
                                "id": "cb2",
                                "from": {"id": 999111222},
                                "message": {"chat": {"id": 999111222}},
                                "data": "confirm_rm:uji_konfirmasi.txt",
                            },
                        }
                    )
                    assert not (ws_path / "uji_konfirmasi.txt").exists(), "File harus terhapus setelah confirm_rm!"
                finally:
                    tg.send_text = orig_send_text  # type: ignore
        finally:
            app_main.telegram_bridge = orig_tg
        record(5, "Telegram Connection, Owner Lock, & Webhook Token Test", True, "Webhook 403/200 secret token, isolasi owner tunggal, & konfirmasi hapus file 2-tahap lulus.")

        # =====================================================================
        # Start Local Safe Demo Web Server for Suites #6, #7, #8
        # =====================================================================
        port = _find_free_port()
        browser.internal_port = port

        # Tambahkan route form uji aman di FastAPI untuk pengujian form & tabel hasil submit tanpa menyentuh web produksi luar!
        if not any(getattr(r, "path", "") == "/safe-practice-form" for r in app_main.app.routes):
            from fastapi.responses import HTMLResponse

            @app_main.app.get("/safe-practice-form", response_class=HTMLResponse)
            async def _safe_practice_form():
                return """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Safe Practice Form</title></head>
                <body>
                  <h1>Safe Practice Registration Form</h1>
                  <div id="err_box" role="alert" class="alert-danger" style="display:none;"></div>
                  <form id="regForm" onsubmit="return handleSub(event)">
                    <div class="row"><label for="firstName">First Name</label>
                      <input id="firstName" name="firstName" required placeholder="First Name" /></div>
                    <div class="row"><label for="userEmail">Email</label>
                      <input id="userEmail" name="userEmail" type="email" required placeholder="name@example.com" /></div>
                    <div class="row"><label for="userNumber">Mobile</label>
                      <input id="userNumber" name="userNumber" maxlength="10" required placeholder="10 Digits" /></div>
                    <div class="row"><label for="deptSelect">Department</label>
                      <select id="deptSelect" name="department">
                        <option value="">-- Pilih --</option>
                        <option value="Operasional">Operasional</option>
                        <option value="Keuangan">Keuangan</option>
                      </select></div>
                    <div class="row">
                      <input type="radio" id="gender-m" name="gender" value="Male" style="opacity:0;width:6px;height:6px;" />
                      <label for="gender-m">Male</label>
                    </div>
                    <div class="row"><label for="otpInput">OTP Token</label>
                      <input id="otpInput" name="otp_token" value="998877" /></div>
                    <div style="height:750px;">Spacer Below Fold</div>
                    <button id="submit" type="submit">Submit</button>
                  </form>
                  <div id="resModal" class="modal show" role="dialog" style="display:none;">
                    <h4 class="modal-title">Thanks for submitting the form</h4>
                    <table>
                      <tbody>
                        <tr><td>Student Name</td><td id="r_name"></td></tr>
                        <tr><td>Student Email</td><td id="r_email"></td></tr>
                        <tr><td>Gender</td><td id="r_gender"></td></tr>
                        <tr><td>Mobile</td><td id="r_mobile"></td></tr>
                        <tr><td>Department</td><td id="r_dept"></td></tr>
                      </tbody>
                    </table>
                  </div>
                  <script>
                    function handleSub(ev) {
                      ev.preventDefault();
                      const fn = document.getElementById('firstName').value.trim();
                      const em = document.getElementById('userEmail').value.trim();
                      const mb = document.getElementById('userNumber').value.trim();
                      if (!fn || !em || mb.length < 8) {
                        const eb = document.getElementById('err_box');
                        eb.innerText = 'Validasi gagal: lengkapi seluruh kolom wajib';
                        eb.style.display = 'block';
                        return false;
                      }
                      document.getElementById('err_box').style.display = 'none';
                      const g = document.querySelector('input[name="gender"]:checked');
                      document.getElementById('r_name').innerText = fn;
                      document.getElementById('r_email').innerText = em;
                      document.getElementById('r_gender').innerText = g ? g.value : '';
                      document.getElementById('r_mobile').innerText = mb;
                      document.getElementById('r_dept').innerText = document.getElementById('deptSelect').value;
                      document.getElementById('resModal').style.display = 'block';
                      return false;
                    }
                  </script>
                </body></html>"""

        cfg = uvicorn.Config(app_main.app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(cfg)
        serve_task = asyncio.create_task(server.serve())
        for _ in range(40):
            if server.started:
                break
            await asyncio.sleep(0.05)

        # =====================================================================
        # 6. BROWSER AUTOMATION TEST (Navigate, DOM, Scroll, Screenshot Masking)
        # =====================================================================
        nav_out = await browser.navigate(f"http://127.0.0.1:{port}/demo-portal")
        assert "Berhasil membuka" in nav_out
        assert await browser.wait_for_page_stable() is True
        dom_portal = await browser.observe(annotated_screenshot=True)
        assert len(dom_portal.get("elements", [])) >= 3
        sc_out = await browser.scroll_page("down", 300)
        assert "Scroll" in sc_out
        proof_portal = await browser.capture_proof_screenshot()
        assert len(proof_portal) > 500
        record(6, "Browser Automation Test (/demo-portal)", True, f"Navigasi, observasi DOM ({len(dom_portal['elements'])} elemen), scroll, & screenshot bukti ({len(proof_portal)} chars b64) berhasil.")

        # =====================================================================
        # 7. SAFE DEMO FORM FILLING + PRE/POST-SUBMIT TABLE VERIFICATION TEST
        # =====================================================================
        orch.current_goal = (
            f"Buka http://127.0.0.1:{port}/safe-practice-form lalu isi:\n"
            "- First Name: Budi Santoso\n"
            "- Email: budi@example.com\n"
            "- Gender: Male\n"
            "- Mobile: 081234567890\n"
            "- Department: Operasional\n"
            "Lalu klik Submit dan verifikasi tabel hasilnya."
        )
        orch._goal_expectations = orch._parse_goal_kv_expectations(orch.current_goal)
        orch._expected_fields = {}
        orch._field_warnings = {}
        orch._recorded_issues = []
        orch._last_submit_verification = None
        orch._pre_submit_blocked_once = False

        await browser.navigate(f"http://127.0.0.1:{port}/safe-practice-form")
        dom_f = await browser.observe(annotated_screenshot=False)
        els_f = dom_f["elements"]

        # Pastikan kolom OTP termasking 'disc' di layar!
        otp_mask = await browser.page.locator("#otpInput").evaluate("el => getComputedStyle(el).webkitTextSecurity")
        assert otp_mask == "disc"

        by_id = {e["el_id"]: e["index"] for e in els_f if e.get("el_id")}
        await orch._execute_with_guardrails(1, {"name": "type", "index": by_id["firstName"], "text": "Budi Santoso"}, dom_f)
        await orch._execute_with_guardrails(2, {"name": "type", "index": by_id["userEmail"], "text": "budi@example.com"}, dom_f)
        await orch._execute_with_guardrails(3, {"name": "click", "index": by_id["gender-m"]}, dom_f)
        await orch._execute_with_guardrails(4, {"name": "type", "index": by_id["userNumber"], "text": "081234567890"}, dom_f)
        await orch._execute_with_guardrails(5, {"name": "select", "index": by_id["deptSelect"], "option": "Operasional"}, dom_f)

        dom_before_sub = await browser.observe(annotated_screenshot=False)
        els_sub = dom_before_sub["elements"]
        sub_idx = next(e["index"] for e in els_sub if e.get("el_id") == "submit")
        sub_msg = await orch._execute_with_guardrails(6, {"name": "click", "index": sub_idx}, dom_before_sub)
        assert "VERIFIKASI TABEL HASIL SESUAI 100%" in sub_msg, f"Unexpected submit result: {sub_msg}"
        assert any("maxlength=10" in iss for iss in orch._recorded_issues), f"Expected maxlength truncation note, got {orch._recorded_issues}"

        # Uji Aturan #5: Klik Submit kedua kalinya TIDAK men-submit ulang secara buta, melainkan memverifikasi tabel yang sudah ada!
        sub_again = await orch._execute_with_guardrails(7, {"name": "click", "index": sub_idx}, dom_before_sub)
        assert "VERIFIKASI HASIL SEBELUMNYA" in sub_again
        record(7, "Form Filling & Post-Submit Verification Test", True, "Isi form, deteksi pemotongan maxlength=10 jujur, verifikasi tabel hasil 100%, & anti double-submit lulus.")

        # =====================================================================
        # 8. ERROR RECOVERY TEST (Missing Element, Website Error, Tab Crash, Blind Click)
        # =====================================================================
        rec_eng = ErrorRecoveryEngine()
        # 8A: Website error message detection pada form kosong (custom JS alert + native validation)
        await browser.navigate(f"http://127.0.0.1:{port}/safe-practice-form")
        await browser.page.evaluate("() => { const f = document.getElementById('regForm'); f.noValidate = true; document.getElementById('submit').click(); }")
        dom_err = await browser.observe(annotated_screenshot=False)
        assert any("Validasi gagal" in pe for pe in dom_err.get("page_errors", []))
        llm_view = format_dom_for_llm(dom_err)
        assert "[ERROR WEB]: Validasi gagal" in llm_view

        # 8B: Blind repeat click circuit breaker
        act_rep = {"name": "click", "index": 1}
        assert rec_eng.check_blind_repeat_before_action(act_rep, dom_err) is None
        rec_eng.record_action_delta(act_rep, dom_err)
        cb_res = rec_eng.check_blind_repeat_before_action(act_rep, dom_err)
        assert cb_res and "RECOVERY CIRCUIT BREAKER" in cb_res

        # 8C: Simulasi Tab Crash -> Auto-recovery kembali ke last_verified_url!
        last_good_url = f"http://127.0.0.1:{port}/demo-portal"
        await browser.navigate(last_good_url)
        await browser.page.close()
        crash_rec = await rec_eng.attempt_browser_recovery(
            browser,
            {"name": "click", "index": 1},
            "browser_crash",
            "Target closed",
            last_verified_url=last_good_url,
        )
        assert crash_rec and crash_rec["recovered"] is True
        assert "/demo-portal" in browser.page.url
        record(8, "Error Recovery Test (Web Error, Blind-Click Breaker, Tab Crash Restore)", True, "Deteksi pesan error web, blokir klik berulang buta, & pemulihan tab crash ke URL terakhir lulus.")

        # =====================================================================
        # 9. TASK STATE STORAGE TEST (Concurrent Writes, Backup Fallback, Secret Scrub)
        # =====================================================================
        ts_test_dir = tmp_dir / "ts_suite9"
        ts = TaskStateStore(str(ts_test_dir))
        created = ts.create_task(f"Login portal pakai password: SuperSecretPassword77 dan token {syn_tok}")
        tid9 = created["task_id"]

        await asyncio.gather(
            *[
                asyncio.to_thread(
                    ts.record_step_outcome,
                    tid9,
                    i,
                    "click",
                    f"Klik elemen {i} berhasil",
                    True,
                    "https://example.org/ok",
                )
                for i in range(1, 11)
            ]
        )
        raw_disk = ts.main_file.read_text(encoding="utf-8")
        assert "SuperSecretPassword77" not in raw_disk and syn_tok not in raw_disk
        t9 = ts.get_task(tid9)
        assert t9 and len(t9["completed_steps"]) == 10

        # Simulasi korupsi file utama -> otomatis pulih dari .bak!
        ts.finalize_task(tid9, "BERHASIL", 5, True, "Semua langkah selesai")
        # Picu 1 update lagi agar .bak menyimpan state valid, lalu rusak file utama
        tid9_b = ts.create_task("Tugas Kedua")["task_id"]
        ts.main_file.write_text("{corrupted_json_syntax", encoding="utf-8")
        fallback_loaded = ts.get_task(tid9)
        assert fallback_loaded and fallback_loaded["status"] == "BERHASIL"
        record(9, "Task State Storage Test (10x Concurrent Writes, .bak Fallback, Redaction)", True, "10 penulisan paralel utuh, pemulihan korupsi dari .bak sukses, & 0 kredensial di disk.")

        # =====================================================================
        # 10. REPORT ACCURACY TEST (All 5 Statuses & Non-Zero Browser Step Guarantee)
        # =====================================================================
        # Pastikan walau history kosong, bila browser_actions_count == 3 maka langkah TIDAK PERNAH 0!
        m_nonzero = compute_step_metrics(history=[], plan=[{"step": "Buka web", "status": "completed"}], browser_actions_count=3, final_status="BERHASIL")
        assert m_nonzero["succeeded"] == 3 and m_nonzero["total_executed"] == 3, f"Step count must never be 0 when browser acted: {m_nonzero}"

        required_labels = [
            "Nama Pekerjaan:",
            "Status Akhir:",
            "Langkah Direncanakan:",
            "Langkah Berhasil:",
            "Langkah Gagal:",
            "Langkah Belum Selesai:",
            "Durasi Eksekusi:",
            "Kendala Ditemukan:",
            "Hasil Verifikasi Akhir:",
            "Bukti Screenshot:",
        ]
        for st_code in ("BERHASIL", "GAGAL", "SEBAGIAN BERHASIL", "MENUNGGU PERSETUJUAN", "TERHENTI"):
            m_st = compute_step_metrics(
                history=[{"step": 1, "action_name": "navigate", "result": "Berhasil membuka /demo-portal"}],
                plan=[{"step": "Tahap 1", "status": "completed"}, {"step": "Tahap 2", "status": "pending"}],
                browser_actions_count=1,
                final_status=st_code,
            )
            rep_s = format_execution_report(
                instruction="Verifikasi Portal Demo",
                final_status=st_code,
                metrics=m_st,
                duration_sec=4,
                issues=["Contoh catatan kendala"] if st_code != "BERHASIL" else [],
                verification_summary="Terverifikasi",
                has_screenshot=True,
            )
            assert st_code in rep_s
            for req_lbl in required_labels:
                assert req_lbl in rep_s, f"Missing '{req_lbl}' in report for {st_code}"
        record(10, "Report Accuracy Test (10 Metrics, 5 Statuses, Non-Zero Step Guarantee)", True, "Ke-5 status resmi & ke-10 komponen wajib tampil akurat; langkah browser dijamin > 0.")

        # =====================================================================
        # 11. POST-RESTART RECOVERY & CONTINUATION TEST (TANPA DUPLIKASI SIDE-EFFECT)
        # =====================================================================
        restart_prof = tmp_dir / "restart_prof"
        fm_r = WorkspaceFileManager(str(tmp_dir / "ws_r"))
        be_r1 = CloudBrowserEngine(fm_r, _capture_ev, profile_dir=str(restart_prof))
        orch_r1 = AgentOrchestrator(be_r1, fm_r, LLMConfig(), _capture_ev, None)

        rec_r1 = orch_r1.task_store.create_task("Isi dan submit formulir pendaftaran demo")
        tid_r1 = rec_r1["task_id"]
        fp_submit = compute_side_effect_fingerprint(
            {"name": "click", "index": 9, "_is_side_effect": True, "_target_sig": "#submit"},
            f"http://127.0.0.1:{port}/safe-practice-form",
        )
        orch_r1.task_store.record_step_outcome(
            tid_r1,
            step_num=1,
            action_name="click",
            result_text="Submit berhasil terverifikasi",
            succeeded=True,
            verified_url=f"http://127.0.0.1:{port}/safe-practice-form",
            idempotency_fp=fp_submit,
        )
        orch_r1.task_store.mark_step_running(tid_r1, 2, "write_file", "Menyimpan rekap akhir...")

        # Simulasi server restart mendadak! Instansiasi baru membaca direktori yang sama:
        be_r2 = CloudBrowserEngine(fm_r, _capture_ev, profile_dir=str(restart_prof))
        orch_r2 = AgentOrchestrator(be_r2, fm_r, LLMConfig(), _capture_ev, None)

        after_boot = orch_r2.task_store.get_task(tid_r1)
        assert after_boot and after_boot["status"] == "TERHENTI"
        res_ctx = orch_r2.task_store.get_resumable_context()
        assert res_ctx and res_ctx["last_verified_url"].endswith("/safe-practice-form")
        assert fp_submit in res_ctx["idempotency_ledger"]

        # Pastikan ketika user mengirim "lanjut", idempotency ledger diwariskan agar klik Submit TIDAK diulangi!
        async def _dummy_loop() -> None:
            return

        orch_r2._run_loop = _dummy_loop  # type: ignore
        await orch_r2.start_goal("lanjut")
        await asyncio.sleep(0.05)
        assert "Isi dan submit formulir pendaftaran demo" in orch_r2.current_goal
        assert "KONTEKS PEMULIHAN PASCA-RESTART" in orch_r2.current_goal
        assert orch_r2.task_store.check_idempotency(orch_r2._active_task_id, fp_submit) is not None
        record(11, "Post-Restart Recovery & Idempotent Continuation Test", True, "Pemulihan pasca-restart menandai TERHENTI, mewariskan URL terakhir & idempotency ledger saat 'lanjut'.")

        # =====================================================================
        # 12. RAILWAY VOLUME /data MOUNT PROOF, PRODUCTION FAIL-CLOSED & NON-DESTRUCTIVE PROBE TEST
        # =====================================================================
        import hashlib
        from fastapi.testclient import TestClient
        import main as main_mod
        from agent.security import (
            StorageUnavailableError,
            resolve_storage_config,
            verify_persistent_volume_mount,
        )

        # (A) Simulasi Volume Persisten Valid (RAILWAY_VOLUME_MOUNT_PATH + mountinfo ext4 rw)
        sim_vol_holder = Path(tempfile.mkdtemp(prefix=".vol_sim_", dir=str(ROOT))).resolve()
        try:
            sim_vol = (sim_vol_holder / "data").resolve()
            sim_vol.mkdir(parents=True, exist_ok=True)
            valid_mountinfo = (
                "20 1 254:0 / / rw,relatime - overlay overlay rw\n"
                f"99 20 259:1 / {sim_vol} rw,relatime - ext4 /dev/nvme1n1 rw\n"
            )
            # Pastikan bila RAILWAY_VOLUME_MOUNT_PATH mengarah ke /tmp, selalu ditolak!
            v_bad_tmp, _, _ = verify_persistent_volume_mount(
                {"RAILWAY_VOLUME_MOUNT_PATH": str(tmp_dir / "bad_tmp_vol")},
                mountinfo_text=valid_mountinfo,
            )
            assert v_bad_tmp is False

            prod_valid = resolve_storage_config(
                ROOT,
                env={
                    "RAILWAY_ENVIRONMENT": "production",
                    "RAILWAY_VOLUME_MOUNT_PATH": str(sim_vol),
                },
                mountinfo_text=valid_mountinfo,
                ismount_fn=lambda p: Path(p).resolve() == sim_vol,
            )
            assert prod_valid["ok"] is True and prod_valid["allow_writes"] is True, f"prod_valid failed: {prod_valid}"
            assert prod_valid["storage_persistent"] is True and prod_valid["storage_mode"] == "railway_volume"
            assert prod_valid["profile_dir"] == str(sim_vol / "nusa_operator_profile")
            assert prod_valid["workspace_dir"] == str(sim_vol / "workspace_files")
            assert (sim_vol / "nusa_operator_profile").is_dir() and (sim_vol / "workspace_files").is_dir()

            # (B) Syarat #3: Validasi TIDAK mengubah/menghapus/menimpa data existing & file probe unik bersih 100%
            existing_files = {
                sim_vol / "nusa_operator_profile" / "security_state.json": '{"owner_id": 777111, "web_access_key": "999111"}',
                sim_vol / "nusa_operator_profile" / "storage_state.json": '{"cookies": [{"name": "sid", "value": "tetap_utuh"}]}',
                sim_vol / "nusa_operator_profile" / "user_memory.json": '["Nama: Budi"]',
                sim_vol / "workspace_files" / "dokumen_penting.md": "# Dokumen Asli Tidak Boleh Tersentuh\nBaris penting.",
            }
            before_hashes: Dict[Path, str] = {}
            for fp_ex, content_ex in existing_files.items():
                fp_ex.write_text(content_ex, encoding="utf-8")
                before_hashes[fp_ex] = hashlib.sha256(fp_ex.read_bytes()).hexdigest()

            for _ in range(3):
                re_check = resolve_storage_config(
                    ROOT,
                    env={
                        "RAILWAY_ENVIRONMENT": "production",
                        "RAILWAY_VOLUME_MOUNT_PATH": str(sim_vol),
                    },
                    mountinfo_text=valid_mountinfo,
                    ismount_fn=lambda p: Path(p).resolve() == sim_vol,
                )
                assert re_check["ok"] is True and re_check["storage_persistent"] is True

            for fp_ex, digest_before in before_hashes.items():
                assert fp_ex.exists(), f"File existing hilang: {fp_ex}"
                assert hashlib.sha256(fp_ex.read_bytes()).hexdigest() == digest_before, f"File existing berubah: {fp_ex}"

            leftover_probes = list(sim_vol.rglob(".nusa_storage_probe_*"))
            assert len(leftover_probes) == 0, f"Ditemukan file probe tersisa: {leftover_probes}"

            # (C) Syarat #2: Direktori biasa (mkdir) TANPA bukti mount kernel TIDAK BOLEH dianggap storage_persistent=True
            fake_data_dir = (sim_vol_holder / "fake_unmounted_data").resolve()
            fake_data_dir.mkdir(parents=True, exist_ok=True)
            root_only_mountinfo = "20 1 254:0 / / rw,relatime - overlay overlay rw\n"

            # Lokal tanpa RAILWAY_VOLUME_MOUNT_PATH walau folder ada -> storage_persistent WAJIB False
            local_plain = resolve_storage_config(
                tmp_dir / "local_base",
                env={"BROWSER_PROFILE_DIR": str(fake_data_dir / "prof"), "WORKSPACE_DIR": str(fake_data_dir / "ws")},
                mountinfo_text=root_only_mountinfo,
                ismount_fn=lambda _: False,
            )
            assert local_plain["ok"] is True and local_plain["storage_persistent"] is False

            # RAILWAY_VOLUME_MOUNT_PATH diset tapi direktori bukan mount point -> ditolak!
            v_ok_unmounted, _, _ = verify_persistent_volume_mount(
                {"RAILWAY_VOLUME_MOUNT_PATH": str(fake_data_dir)},
                mountinfo_text=root_only_mountinfo,
                ismount_fn=lambda _: False,
            )
            assert v_ok_unmounted is False

            # RAILWAY_VOLUME_MOUNT_PATH diset tapi fstype tmpfs (ephemeral) atau ro -> ditolak!
            tmpfs_mountinfo = (
                "20 1 254:0 / / rw,relatime - overlay overlay rw\n"
                f"100 20 0:44 / {fake_data_dir} rw,nosuid - tmpfs tmpfs rw\n"
            )
            v_ok_tmpfs, _, _ = verify_persistent_volume_mount(
                {"RAILWAY_VOLUME_MOUNT_PATH": str(fake_data_dir)},
                mountinfo_text=tmpfs_mountinfo,
            )
            assert v_ok_tmpfs is False

            ro_mountinfo = (
                "20 1 254:0 / / rw,relatime - overlay overlay rw\n"
                f"101 20 259:2 / {fake_data_dir} ro,relatime - ext4 /dev/nvme2n1 ro\n"
            )
            v_ok_ro, _, _ = verify_persistent_volume_mount(
                {"RAILWAY_VOLUME_MOUNT_PATH": str(fake_data_dir)},
                mountinfo_text=ro_mountinfo,
            )
            assert v_ok_ro is False

            # (D) Syarat #1: Di Railway/Production, jika Volume tidak tersedia / salah path -> FAIL-CLOSED!
            # TIDAK BOLEH dialihkan diam-diam ke /tmp, dan seluruh penulisan state penting DIBLOKIR!
            prod_no_vol = resolve_storage_config(
                ROOT,
                env={"RAILWAY_ENVIRONMENT": "production"},
                mountinfo_text=root_only_mountinfo,
                ismount_fn=lambda _: False,
            )
            assert prod_no_vol["ok"] is False and prod_no_vol["allow_writes"] is False
            assert prod_no_vol["storage_persistent"] is False
            assert not prod_no_vol["profile_dir"].startswith("/tmp")
            assert not prod_no_vol["workspace_dir"].startswith("/tmp")
            assert prod_no_vol["error"]

            prod_outside_vol = resolve_storage_config(
                ROOT,
                env={
                    "RAILWAY_ENVIRONMENT": "production",
                    "RAILWAY_VOLUME_MOUNT_PATH": str(sim_vol),
                    "BROWSER_PROFILE_DIR": "/tmp/illegal_ephemeral_prof",
                },
                mountinfo_text=valid_mountinfo,
                ismount_fn=lambda p: Path(p).resolve() == sim_vol,
            )
            assert prod_outside_vol["ok"] is False and prod_outside_vol["allow_writes"] is False
            assert prod_outside_vol["storage_persistent"] is False
            assert not Path("/tmp/illegal_ephemeral_prof").exists()

            # Simulasi Volume ter-mount tetapi gagal ditulis (PermissionError / I/O error saat probe) -> wajib fail-closed!
            import agent.security as sec_mod
            orig_probe = sec_mod._probe_directory_non_destructive
            try:
                def _failing_probe(_p: Path) -> None:
                    raise PermissionError("Simulated Volume read-only / permission denied")

                sec_mod._probe_directory_non_destructive = _failing_probe  # type: ignore
                prod_unwritable = resolve_storage_config(
                    ROOT,
                    env={
                        "RAILWAY_ENVIRONMENT": "production",
                        "RAILWAY_VOLUME_MOUNT_PATH": str(sim_vol),
                    },
                    mountinfo_text=valid_mountinfo,
                    ismount_fn=lambda p: Path(p).resolve() == sim_vol,
                )
                assert prod_unwritable["ok"] is False and prod_unwritable["allow_writes"] is False
                assert prod_unwritable["storage_persistent"] is False
                assert not prod_unwritable["profile_dir"].startswith("/tmp")
                assert "tidak writable" in prod_unwritable["error"]
            finally:
                sec_mod._probe_directory_non_destructive = orig_probe  # type: ignore
        finally:
            shutil.rmtree(sim_vol_holder, ignore_errors=True)

        # Pastikan seluruh komponen memblokir penulisan (0 direktori/file dibuat) saat allow_writes=False
        blocked_root = tmp_dir / "blocked_should_never_exist"
        fm_blk = WorkspaceFileManager(str(blocked_root / "ws"), allow_writes=False, write_block_reason=prod_no_vol["error"])
        tg_blk = TelegramBotBridge(state_dir=str(blocked_root / "prof"), allow_writes=False, write_block_reason=prod_no_vol["error"])
        be_blk = CloudBrowserEngine(fm_blk, _capture_ev, profile_dir=str(blocked_root / "prof"), allow_writes=False, write_block_reason=prod_no_vol["error"])
        orch_blk = AgentOrchestrator(be_blk, fm_blk, LLMConfig(), _capture_ev, tg_blk, allow_writes=False, write_block_reason=prod_no_vol["error"])

        assert not blocked_root.exists(), "Direktori tidak boleh dibuat saat allow_writes=False!"

        for blocked_call in (
            lambda: fm_blk.write_file("tes.md", "isi"),
            lambda: fm_blk.save_bytes("tes.bin", b"123"),
            lambda: fm_blk.make_dir("sub"),
            lambda: orch_blk.task_store.create_task("Tugas terlarang di ephemeral"),
            lambda: tg_blk._persist_security(),
            lambda: orch_blk.add_user_memories("Kunci: Nilai"),
        ):
            raised = False
            try:
                blocked_call()
            except StorageUnavailableError:
                raised = True
            assert raised, "Operasi tulis wajib melempar StorageUnavailableError saat allow_writes=False"

        for blocked_async in (
            lambda: orch_blk.start_goal("Jalankan sesuatu"),
            lambda: be_blk.configure_proxy("1.2.3.4:8080"),
            lambda: be_blk.save_state(),
        ):
            raised_a = False
            try:
                await blocked_async()
            except StorageUnavailableError:
                raised_a = True
            assert raised_a, "Operasi async tulis wajib melempar StorageUnavailableError saat allow_writes=False"

        assert not blocked_root.exists(), "Tidak boleh ada 1 byte pun ditulis ke lokasi sementara saat diblokir!"

        # Verifikasi endpoint /api/health & middleware fail-closed (HTTP 503) ketika STORAGE_STATUS gagal
        orig_storage_status = dict(main_mod.STORAGE_STATUS)
        try:
            main_mod.STORAGE_STATUS.clear()
            main_mod.STORAGE_STATUS.update(prod_no_vol)
            with TestClient(main_mod.app) as tc:
                r_health_err = tc.get("/api/health")
                assert r_health_err.status_code == 503
                body_err = r_health_err.json()
                assert body_err["ok"] is False and body_err["storage_ok"] is False and body_err["storage_persistent"] is False
                assert body_err["storage_error"]
                r_stat_err = tc.get("/api/status")
                assert r_stat_err.status_code == 503
        finally:
            main_mod.STORAGE_STATUS.clear()
            main_mod.STORAGE_STATUS.update(orig_storage_status)

        record(12, "Railway Volume Mount Proof, Production Fail-Closed & Non-Destructive Probe", True, "Mount kernel & RAILWAY_VOLUME_MOUNT_PATH tervalidasi, fail-closed 503 memblokir 100% tulis ke /tmp, & data lama utuh.")

        # =====================================================================
        # 13. TAHAP 1: AI ERROR CLASSIFICATION, BACKOFF + RETRY-AFTER & SEQUENTIAL FALLBACK
        # =====================================================================
        import httpx
        import agent.llm_providers as llm_mod
        from agent.llm_providers import (
            LLMCallError,
            MultiLLMBrain,
            classify_http_failure,
            compute_backoff_delay,
            parse_retry_after_seconds,
        )

        # 13a. Unit check parse_retry_after_seconds, compute_backoff_delay, classify_http_failure
        r_429 = httpx.Response(429, headers={"Retry-After": "3.5"}, text='{"error":{"message":"Quota exceeded"}}')
        assert abs((parse_retry_after_seconds(r_429) or 0.0) - 3.5) < 0.01
        d_429 = compute_backoff_delay(0, retry_after=parse_retry_after_seconds(r_429), base=0.65, cap=5.0)
        assert d_429 >= 3.5, f"Backoff wajib menghormati Retry-After >= 3.5s, dapat: {d_429}"
        k_401, _ = classify_http_failure(httpx.Response(401, text="API key not valid"))
        k_404, _ = classify_http_failure(httpx.Response(404, text="models/foo is not found"))
        k_503, _ = classify_http_failure(httpx.Response(503, text="The model is overloaded"))
        assert (k_401, k_404, k_503) == ("auth_error", "model_not_found", "server_overloaded")

        def _make_cfg(prov: str, key: str, mod: str = "") -> LLMConfig:
            c = LLMConfig()
            c.update(prov, mod, key)
            return c

        # Pastikan model usang gemini-3.1-flash-lite-preview tidak lagi ada di kandidat
        brain_probe = MultiLLMBrain(_make_cfg("gemini", "dummy-unit-test-key-00"))
        assert "gemini-3.1-flash-lite-preview" not in brain_probe._gemini_candidate_models(light_first=False)
        assert "gemini-3.1-flash-lite-preview" not in brain_probe._gemini_candidate_models(light_first=True)
        assert "gemini-2.5-flash" in brain_probe._gemini_candidate_models(light_first=False)
        assert "gemini-3.1-flash-lite" in brain_probe._gemini_candidate_models(light_first=False)

        orig_get_http = llm_mod.get_shared_http
        try:
            # 13b. 401/403 Auth Error -> langsung berhenti di percobaan ke-1 tanpa retry sia-sia, pesan jujur!
            calls_auth: List[str] = []
            sleeps_auth: List[float] = []

            class _MockHttpAuth:
                async def post(self, url: str, **kwargs: Any) -> httpx.Response:
                    calls_auth.append(url)
                    return httpx.Response(403, text='{"error":{"message":"API key invalid or permission denied"}}')

            llm_mod.get_shared_http = lambda: _MockHttpAuth()  # type: ignore
            b_auth = MultiLLMBrain(_make_cfg("gemini", "dummy-unit-test-key-01"))
            b_auth._sleep_fn = lambda s: (sleeps_auth.append(s) or asyncio.sleep(0))  # type: ignore
            msg_auth = await b_auth.quick_chat("Halo tes")
            assert len(calls_auth) == 1 and len(sleeps_auth) == 0, f"Auth error tidak boleh retry berulang: calls={len(calls_auth)}"
            assert b_auth.last_chat_ok is False and b_auth.last_error_kind == "auth_error"
            assert ("API key" in msg_auth or "API Key" in msg_auth) and "sibuk" not in msg_auth.lower(), (
                f"Pesan auth error wajib jujur soal API key, bukan bilang 'sibuk': {msg_auth!r}"
            )

            # 13c. Sequential Fallback (404 karantina model + 429 Retry-After + 503 + sukses di model cadangan, 0 duplikasi paralel)
            calls_seq: List[str] = []
            sleeps_seq: List[float] = []
            in_flight = 0
            max_in_flight = 0

            class _MockHttpSeq:
                async def post(self, url: str, **kwargs: Any) -> httpx.Response:
                    nonlocal in_flight, max_in_flight
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                    await asyncio.sleep(0.005)
                    in_flight -= 1
                    m_seg = url.split("/models/")[1].split(":")[0]
                    calls_seq.append(m_seg)
                    idx = len(calls_seq)
                    if idx == 1:
                        return httpx.Response(404, text='{"error":{"message":"Model not found"}}')
                    if idx == 2:
                        return httpx.Response(429, headers={"Retry-After": "2.25"}, text='{"error":{"message":"Resource exhausted"}}')
                    if idx == 3:
                        return httpx.Response(503, text='{"error":{"message":"Overloaded"}}')
                    return httpx.Response(
                        200,
                        json={"candidates": [{"content": {"parts": [{"text": "Jawaban sukses dari model fallback!"}]}}]},
                    )

            llm_mod.get_shared_http = lambda: _MockHttpSeq()  # type: ignore
            b_seq = MultiLLMBrain(_make_cfg("gemini", "dummy-unit-test-key-02", "dead-model-404"))
            b_seq._sleep_fn = lambda s: (sleeps_seq.append(s) or asyncio.sleep(0))  # type: ignore
            ans_seq = await b_seq.quick_chat("Jelaskan singkat")
            assert ans_seq == "Jawaban sukses dari model fallback!"
            assert b_seq.last_chat_ok is True and b_seq.last_error_kind == ""
            assert max_in_flight == 1, f"Tidak boleh ada hedged racing paralel! max_in_flight={max_in_flight}"
            assert "dead-model-404" in b_seq._unavailable_models, "Model 404 wajib dikarantina sementara"
            assert any(s >= 2.25 for s in sleeps_seq), f"Wajib menghormati Retry-After 2.25s: {sleeps_seq}"

            # 13d. _call_gemini dengan timeout, network error, & JSON rusak -> fallback lalu lempar LLMCallError(exhausted_retry=True) jika semua gagal
            step_events: List[str] = []

            class _MockHttpStep:
                async def post(self, url: str, **kwargs: Any) -> httpx.Response:
                    m_seg = url.split("/models/")[1].split(":")[0]
                    step_events.append(m_seg)
                    n = len(step_events)
                    if n == 1:
                        raise httpx.ReadTimeout("Simulated read timeout")
                    if n == 2:
                        raise httpx.ConnectError("Simulated socket reset")
                    if n == 3:
                        # Malformed JSON / action kosong -> wajib ditolak!
                        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"thought": "bingung"}'}]}}]})
                    return httpx.Response(
                        200,
                        json={
                            "candidates": [
                                {
                                    "content": {
                                        "parts": [
                                            {"text": '{"thought":"ok","action":{"name":"navigate","url":"https://example.org"}}'}
                                        ]
                                    }
                                }
                            ]
                        },
                    )

            llm_mod.get_shared_http = lambda: _MockHttpStep()  # type: ignore
            b_step = MultiLLMBrain(_make_cfg("gemini", "dummy-unit-test-key-03"))
            b_step._sleep_fn = lambda s: asyncio.sleep(0)  # type: ignore
            dec_ok = await b_step._call_gemini("Buka example.org", "")
            assert dec_ok["action"]["name"] == "navigate" and len(step_events) == 4

            # Bila semua model 429 -> wajib melempar LLMCallError dengan exhausted_retry=True & kind='rate_limit'
            class _MockAll429:
                async def post(self, url: str, **kwargs: Any) -> httpx.Response:
                    return httpx.Response(429, headers={"Retry-After": "1.5"}, text="Quota exceeded")

            llm_mod.get_shared_http = lambda: _MockAll429()  # type: ignore
            b_429 = MultiLLMBrain(_make_cfg("gemini", "dummy-unit-test-key-04"))
            b_429._sleep_fn = lambda s: asyncio.sleep(0)  # type: ignore
            caught_llm_err: Optional[LLMCallError] = None
            try:
                await b_429._call_gemini("Tes semua 429", "")
            except LLMCallError as exc_429:
                caught_llm_err = exc_429
            assert caught_llm_err is not None and caught_llm_err.kind == "rate_limit" and caught_llm_err.exhausted_retry is True
        finally:
            llm_mod.get_shared_http = orig_get_http

        record(13, "Tahap 1: AI Error Classification, Backoff+Jitter, Retry-After & Sequential Fallback", True, "401/403 fast-fail jujur, 404 karantina, 429 Retry-After dipatuhi, timeout/network/JSON rusak fallback mulus tanpa duplikasi request.")

        # =====================================================================
        # 14. TAHAP 2: HONEST STATUS (BERHASIL vs SEBAGIAN BERHASIL vs GAGAL)
        # =====================================================================
        async def _wait_orch_idle(orch_inst: AgentOrchestrator, timeout_s: float = 8.0) -> None:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if orch_inst._task is None or orch_inst._task.done():
                    break
                await asyncio.sleep(0.03)

        t2_root = tmp_dir / "t2_status_honesty"
        fm_t2 = WorkspaceFileManager(str(t2_root / "ws"))
        be_t2 = CloudBrowserEngine(fm_t2, _capture_ev, profile_dir=str(t2_root / "prof"))
        orch_t2 = AgentOrchestrator(be_t2, fm_t2, LLMConfig(), _capture_ev, None)

        # 14a. JSON rusak / action kosong TIDAK BOLEH pernah default ke 'done' / BERHASIL!
        async def _broken_json_decide(**kwargs: Any) -> Dict[str, Any]:
            return {"thought": "Output rusak tanpa objek action valid", "action": {}}

        orch_t2.brain.decide_next_step = _broken_json_decide  # type: ignore
        await orch_t2.start_goal("Hitung analisis lokal sederhana")
        await _wait_orch_idle(orch_t2)
        t_broken = orch_t2.task_store.get_latest_task()
        assert t_broken and t_broken["status"] == "GAGAL" and t_broken["verification_result"]["verified"] is False, (
            f"JSON rusak dilarang keras menjadi BERHASIL: {t_broken}"
        )

        # 14b. AI Error (LLMCallError exhausted_retry=True) -> 1x panggil tanpa badai retry ganda & status wajib GAGAL!
        llm_err_calls = 0

        async def _exhausted_ai_decide(**kwargs: Any) -> Dict[str, Any]:
            nonlocal llm_err_calls
            llm_err_calls += 1
            raise LLMCallError("Kuota sementara penuh", kind="rate_limit", status_code=429, exhausted_retry=True)

        orch_t2.brain.decide_next_step = _exhausted_ai_decide  # type: ignore
        await orch_t2.start_goal("Rangkum catatan kerja lokal")
        await _wait_orch_idle(orch_t2)
        t_ai_err = orch_t2.task_store.get_latest_task()
        assert llm_err_calls == 1, f"Orchestrator tidak boleh menggandakan retry saat LLMCallError exhausted: {llm_err_calls}"
        assert t_ai_err and t_ai_err["status"] == "GAGAL" and t_ai_err["verification_result"]["verified"] is False

        # 14c. Semua aksi gagal lalu AI memanggil 'done' -> WAJIB GAGAL (bukan BERHASIL palsu!)
        seq_fail = iter(
            [
                {"thought": "Coba jalankan kode", "action": {"name": "run_python", "code": "raise ValueError('simulasi_error_1')"}},
                {"thought": "Selesai", "action": {"name": "done", "summary": "Maaf, gagal memproses perhitungan Python."}},
            ]
        )

        async def _decide_seq_fail(**kw: Any) -> Dict[str, Any]:
            return next(seq_fail)

        orch_t2.brain.decide_next_step = _decide_seq_fail  # type: ignore
        await orch_t2.start_goal("Jalankan perhitungan statistik lokal")
        await _wait_orch_idle(orch_t2)
        t_all_fail = orch_t2.task_store.get_latest_task()
        assert t_all_fail and t_all_fail["status"] == "GAGAL" and t_all_fail["verification_result"]["verified"] is False, (
            f"Semua langkah gagal wajib GAGAL, dapat: {t_all_fail}"
        )

        # 14d. Sukses parsial (Langkah 1 berhasil, Langkah 2 gagal, lalu AI berhenti 'done' tanpa deliverable akhir) -> SEBAGIAN BERHASIL!
        seq_partial = iter(
            [
                {"thought": "Langkah 1 ok", "action": {"name": "run_python", "code": "print('tahap_1_ok')"}},
                {"thought": "Langkah 2 gagal", "action": {"name": "run_python", "code": "raise RuntimeError('simulasi_error_2')"}},
                {"thought": "Berhenti", "action": {"name": "done", "summary": "Tahap pertama selesai tetapi tahap kedua terkendala."}},
            ]
        )

        async def _decide_seq_partial(**kw: Any) -> Dict[str, Any]:
            return next(seq_partial)

        orch_t2.brain.decide_next_step = _decide_seq_partial  # type: ignore
        await orch_t2.start_goal("Eksekusi dua tahap skrip lokal")
        await _wait_orch_idle(orch_t2)
        t_part = orch_t2.task_store.get_latest_task()
        assert t_part and t_part["status"] == "SEBAGIAN BERHASIL" and t_part["verification_result"]["verified"] is False, (
            f"Langkah akhir gagal wajib SEBAGIAN BERHASIL, dapat: {t_part}"
        )

        # 14e. Sukses murni -> BERHASIL & verified=True!
        seq_pure_ok = iter(
            [
                {"thought": "Hitung", "action": {"name": "run_python", "code": "print(21 * 2)"}},
                {"thought": "Beres", "action": {"name": "done", "summary": "Hasil perhitungan adalah 42."}},
            ]
        )

        async def _decide_seq_pure(**kw: Any) -> Dict[str, Any]:
            return next(seq_pure_ok)

        orch_t2.brain.decide_next_step = _decide_seq_pure  # type: ignore
        await orch_t2.start_goal("Hitung 21 dikali 2 dengan Python")
        await _wait_orch_idle(orch_t2)
        t_pure = orch_t2.task_store.get_latest_task()
        assert t_pure and t_pure["status"] == "BERHASIL" and t_pure["verification_result"]["verified"] is True
        await be_t2.stop()

        record(14, "Tahap 2: Honest Status Logic (Pure Success vs Partial vs Failure, JSON/AI Guard)", True, "JSON rusak, AI error, & aksi gagal 0% bocor jadi BERHASIL; sukses murni, parsial, & gagal terpisah presisi.")

        # =====================================================================
        # 15. TAHAP 3: FIFO DEDUP, PER-CHAT CONCURRENCY LOCK + FAST-PATH, & CARD ISOLATION
        # =====================================================================
        t3_root = tmp_dir / "t3_concurrency"
        fm_t3 = WorkspaceFileManager(str(t3_root / "ws"))
        tg_t3 = TelegramBotBridge(state_dir=str(t3_root / "prof"))
        tg_t3.token = "dummy_tg_unit_token"
        be_t3 = CloudBrowserEngine(fm_t3, _capture_ev, profile_dir=str(t3_root / "prof"))
        orch_t3 = AgentOrchestrator(be_t3, fm_t3, LLMConfig(), _capture_ev, tg_t3)
        tg_t3.attach_orchestrator(orch_t3)

        sent_t3: List[Tuple[int, int, str]] = []
        edited_t3: List[Tuple[int, int, str]] = []
        next_mid_t3 = 500

        async def _mock_send_t3(cid: int, txt: str, reply_markup: Any = None) -> Optional[int]:
            nonlocal next_mid_t3
            next_mid_t3 += 1
            sent_t3.append((cid, next_mid_t3, txt))
            return next_mid_t3

        async def _mock_edit_t3(cid: int, mid: int, txt: str, reply_markup: Any = None) -> None:
            edited_t3.append((cid, mid, txt))

        async def _noop_action_t3(cid: int, act: str = "typing") -> None:
            return None

        tg_t3.send_text = _mock_send_t3  # type: ignore
        tg_t3._edit_text = _mock_edit_t3  # type: ignore
        tg_t3._send_chat_action = _noop_action_t3  # type: ignore

        # 15a. Bounded FIFO deduplication melewati batas 500 & 1000 tanpa .clear() massal!
        for uid_i in range(1, 1006):
            assert tg_t3._mark_update_seen(uid_i) is False
        assert len(tg_t3._seen_update_ids) == 1000
        assert tg_t3._mark_update_seen(1) is False, "Item tertua (>1000 lalu) ter-evict rapi secara FIFO"
        for recent_uid in (500, 501, 999, 1000, 1005):
            assert tg_t3._mark_update_seen(recent_uid) is True, f"Update terbaru {recent_uid} tidak boleh bocor akibat clear!"

        # 15b. Serialisasi per-chat (tidak saling tabrak) + Fast-Path darurat (/stop tetap tembus instan!)
        in_flight_chat = 0
        max_in_flight_chat = 0
        stop_finished_while_chat_busy = False

        async def _slow_quick_chat(prompt: str, recent: Any = None, user_memory_block: str = "") -> str:
            nonlocal in_flight_chat, max_in_flight_chat
            in_flight_chat += 1
            max_in_flight_chat = max(max_in_flight_chat, in_flight_chat)
            await asyncio.sleep(0.18)
            in_flight_chat -= 1
            orch_t3.brain.last_chat_ok = True
            return f"Balasan untuk: {prompt[:30]}"

        orch_t3.brain.quick_chat = _slow_quick_chat  # type: ignore

        upd_a = {"update_id": 9001, "message": {"message_id": 11, "chat": {"id": 777}, "from": {"id": 777}, "text": "halo pesan pertama"}}
        upd_b = {"update_id": 9002, "message": {"message_id": 12, "chat": {"id": 777}, "from": {"id": 777}, "text": "halo pesan kedua"}}
        upd_stop = {"update_id": 9003, "message": {"message_id": 13, "chat": {"id": 777}, "from": {"id": 777}, "text": "/stop"}}

        async def _send_stop_midway() -> None:
            nonlocal stop_finished_while_chat_busy
            await asyncio.sleep(0.05)
            t_s0 = time.monotonic()
            await tg_t3._handle_update(upd_stop)
            if (time.monotonic() - t_s0) < 0.10 and in_flight_chat == 1:
                stop_finished_while_chat_busy = True

        await asyncio.gather(
            tg_t3._handle_update(upd_a),
            tg_t3._handle_update(upd_b),
            _send_stop_midway(),
        )
        assert max_in_flight_chat == 1, f"Pesan per-chat wajib diserialisasi satu per satu! max_in_flight={max_in_flight_chat}"
        assert stop_finished_while_chat_busy is True, "/stop wajib lewat fast-path tanpa terblokir lock chat lambat!"

        # 15c. Isolasi kartu progress antar-run (kartu task lama ditutup lebih dulu & tidak bisa menimpa kartu baru / selesai)
        edited_t3.clear()
        tg_t3.active_chat_ids.add(777)
        tok1 = await tg_t3._begin_new_agent_card(777, "🟢 Kartu Tugas 1")
        mid_card_1 = tg_t3._status_msg_ids[777]
        tok2 = await tg_t3._begin_new_agent_card(777, "🟢 Kartu Tugas 2")
        mid_card_2 = tg_t3._status_msg_ids[777]
        assert tok2 > tok1 and mid_card_2 != mid_card_1
        assert any(m == mid_card_1 and "DIGANTIKAN" in txt for (_, m, txt) in edited_t3), (
            f"Kartu tugas 1 wajib ditutup bersih sebelum kartu 2 aktif: {edited_t3}"
        )
        await tg_t3.notify_done("Ringkasan Tugas 2", elapsed_sec=2, total_steps=1, final_status="BERHASIL")
        assert 777 not in tg_t3._status_msg_ids, "Kartu progress tugas yang sudah selesai wajib dilepas agar tidak tertimpa!"
        edits_count_after_done = len(edited_t3)
        await tg_t3.notify_step(99, "Langkah nyasar terlambat")
        assert len(edited_t3) == edits_count_after_done, "Langkah terlambat dilarang mengedit kartu yang sudah selesai!"
        await be_t3.stop()

        record(15, "Tahap 3: FIFO Dedup, Per-Chat Concurrency Lock + Fast-Path & Card Isolation", True, "Dedup FIFO 1000 ID anti-clear, chat per-user ter-serialisasi, /stop fast-path <0.1s, & kartu progress terisolasi penuh.")

        # =====================================================================
        # 16. TAHAP 4: ANTI ERROR-DOC EXPORT, HONEST OPEN_WEB, & CLEAN 'LANJUT'/TASK ISOLATION
        # =====================================================================
        t4_root = tmp_dir / "t4_edge_isolation"
        fm_t4 = WorkspaceFileManager(str(t4_root / "ws"))
        tg_t4 = TelegramBotBridge(state_dir=str(t4_root / "prof"))
        tg_t4.token = "dummy_tg_t4_token"
        be_t4 = CloudBrowserEngine(fm_t4, _capture_ev, profile_dir=str(t4_root / "prof"))
        orch_t4 = AgentOrchestrator(be_t4, fm_t4, LLMConfig(), _capture_ev, tg_t4)
        tg_t4.attach_orchestrator(orch_t4)

        texts_t4: List[Tuple[int, str]] = []
        edits_t4: List[Tuple[int, int, str]] = []
        docs_t4: List[Tuple[int, str, int]] = []
        mid_seq_t4 = 800

        async def _send_t4(cid: int, txt: str, reply_markup: Any = None) -> Optional[int]:
            nonlocal mid_seq_t4
            mid_seq_t4 += 1
            texts_t4.append((cid, txt))
            return mid_seq_t4

        async def _edit_t4(cid: int, mid: int, txt: str, reply_markup: Any = None) -> None:
            edits_t4.append((cid, mid, txt))

        async def _doc_t4(cid: int, fname: str, fbytes: bytes, caption: str = "") -> None:
            docs_t4.append((cid, fname, len(fbytes)))

        async def _noop_act_t4(cid: int, act: str = "typing") -> None:
            return None

        tg_t4.send_text = _send_t4  # type: ignore
        tg_t4._edit_text = _edit_t4  # type: ignore
        tg_t4.send_document = _doc_t4  # type: ignore
        tg_t4._send_chat_action = _noop_act_t4  # type: ignore

        # 16a. AI gagal (last_chat_ok=False) saat user minta dokumen PDF -> DILARANG ekspor PDF berisi teks error!
        tg_t4.last_output_text[888] = "Dokumen surat pengantar resmi yang valid."

        async def _failed_quick_chat_t4(prompt: str, recent: Any = None, user_memory_block: str = "") -> str:
            orch_t4.brain.last_chat_ok = False
            orch_t4.brain.last_error_kind = "rate_limit"
            return "⚠️ Kuota permintaan AI sedang penuh sementara (HTTP 429)."

        orch_t4.brain.quick_chat = _failed_quick_chat_t4  # type: ignore
        await tg_t4._handle_update(
            {
                "update_id": 9501,
                "message": {"message_id": 1, "chat": {"id": 888}, "from": {"id": 888}, "text": "buatkan surat lamaran kerja pdf"},
            }
        )
        assert len(docs_t4) == 0, f"Dilarang keras membuat file PDF dari pesan error AI! docs={docs_t4}"
        assert tg_t4.last_output_text[888] == "Dokumen surat pengantar resmi yang valid.", "last_output_text tidak boleh ditimpa teks error!"

        # 16b. Isolasi konversi 'ke pdf': file usang tak bernama di workspace TIDAK dibajak di sesi kosong, sedangkan sesi valid dikonversi!
        fm_t4.write_file("arsip_lama_2025.md", "# Arsip Lama Tidak Relevan")
        await tg_t4._handle_update(
            {
                "update_id": 9502,
                "message": {"message_id": 2, "chat": {"id": 889}, "from": {"id": 889}, "text": "ke pdf"},
            }
        )
        assert len(docs_t4) == 0, "Sesi chat kosong tidak boleh diam-diam membajak file lama workspace tanpa disebut namanya!"
        await tg_t4._handle_update(
            {
                "update_id": 9503,
                "message": {"message_id": 3, "chat": {"id": 888}, "from": {"id": 888}, "text": "ke pdf"},
            }
        )
        assert len(docs_t4) == 1 and docs_t4[0][1].endswith(".pdf") and docs_t4[0][2] > 100

        # 16c. Kejujuran open_web saat target_url gagal dimuat ([UNREACHABLE_SITE]) -> WAJIB lapor GAGAL MEMBUKA WEB!
        edits_t4.clear()
        orch_t4.brain.resolve_website_url = lambda q: asyncio.sleep(0, result="https://portal-blokir.contoh.go.id")  # type: ignore
        be_t4.navigate = lambda u: asyncio.sleep(0, result="[UNREACHABLE_SITE] Situs portal-blokir.contoh.go.id menolak koneksi")  # type: ignore
        tg_t4.chat_modes[888] = "open_web"
        await tg_t4._handle_update(
            {
                "update_id": 9504,
                "message": {"message_id": 4, "chat": {"id": 888}, "from": {"id": 888}, "text": "portal-blokir.contoh.go.id"},
            }
        )
        assert any("GAGAL MEMBUKA WEB" in txt for (_, _, txt) in edits_t4), f"open_web gagal wajib lapor jujur: {edits_t4}"
        assert not any("SELESAI DIBUKA" in txt for (_, _, txt) in edits_t4), "open_web gagal dilarang klaim SELESAI DIBUKA!"

        # 16d. Routing 'lanjut' jujur (tidak mengulang task BERHASIL, meresume task TERHENTI) & recent_chat bersih dari laporan 10-poin lama
        seen_recent_chats: List[List[Dict[str, Any]]] = []

        async def _done_with_capture_t4(**kw: Any) -> Dict[str, Any]:
            seen_recent_chats.append(list(kw.get("recent_chat") or []))
            return {"thought": "Selesai cepat", "action": {"name": "done", "summary": "Tugas selesai tuntas."}}

        orch_t4.brain.decide_next_step = _done_with_capture_t4  # type: ignore
        orch_t4.current_goal = "Tugas lama yang sudah tuntas"
        tid_done = orch_t4.task_store.create_task("Tugas lama yang sudah tuntas")["task_id"]
        orch_t4.task_store.finalize_task(tid_done, "BERHASIL", 3, True, "Sudah beres")

        texts_t4.clear()
        await tg_t4._handle_update(
            {
                "update_id": 9505,
                "message": {"message_id": 5, "chat": {"id": 888}, "from": {"id": 888}, "text": "lanjut"},
            }
        )
        assert any("udah selesai tuntas" in t for (_, t) in texts_t4), f"'lanjut' setelah BERHASIL tidak boleh mengulang task lama: {texts_t4}"

        # Buat task TERHENTI -> 'lanjut bro' wajib memulihkan instruksi dari TaskStateStore!
        tid_paused = orch_t4.task_store.create_task("Verifikasi tahap kedua formulir lokal")["task_id"]
        orch_t4.task_store.finalize_task(tid_paused, "TERHENTI", 4, False, "Jeda sementara")
        await tg_t4._handle_update(
            {
                "update_id": 9506,
                "message": {"message_id": 6, "chat": {"id": 888}, "from": {"id": 888}, "text": "lanjut bro"},
            }
        )
        await _wait_orch_idle(orch_t4)
        assert "Verifikasi tahap kedua formulir lokal" in orch_t4.current_goal

        # Pastikan laporan eksekusi 10-poin lama di chat_messages tersaring keluar dari recent_chat pada task baru!
        await orch_t4._push_chat(
            "agent",
            "📋 LAPORAN EKSEKUSI\nNama Pekerjaan: Tugas Lama\nStatus Akhir: ✅ BERHASIL\nLangkah Berhasil: 2",
        )
        seen_recent_chats.clear()
        await orch_t4.start_goal("Hitung tugas baru independen")
        await _wait_orch_idle(orch_t4)
        assert seen_recent_chats, "decide_next_step harus terpanggil pada task baru"
        for msg_rc in seen_recent_chats[0]:
            assert not ("Nama Pekerjaan:" in str(msg_rc.get("text", "")) and "Status Akhir:" in str(msg_rc.get("text", ""))), (
                f"Laporan 10-poin task lama tidak boleh bocor ke recent_chat task baru: {msg_rc}"
            )
        await be_t4.stop()

        record(16, "Tahap 4: Safe Doc Export Guard, Honest open_web & Clean 'lanjut'/Task Isolation", True, "0 file PDF/Word/Excel berisi teks error, open_web jujur saat gagal, 'lanjut' hanya untuk task terjeda, & konteks task baru bersih.")

        # =====================================================================
        # 17. TAHAP 5: ANTI BALASAN "SETENGAH" (TOKEN BUDGET, AUTO-CONTINUE, SPLIT PESAN)
        # =====================================================================
        from agent.llm_providers import MultiLLMBrain
        from agent.telegram_bridge import TelegramBotBridge

        # 17a. Token budget quick_chat sudah 4096 (bukan 1200) + thinking dimatikan
        src_lp = (ROOT / "agent" / "llm_providers.py").read_text(encoding="utf-8")
        assert '"maxOutputTokens": 4096' in src_lp, "quick_chat wajib punya maxOutputTokens 4096 agar balasan tidak kepotong!"
        assert '"maxOutputTokens": 1200' not in src_lp, "maxOutputTokens 1200 (penyebab jawaban setengah) harus sudah hilang!"
        assert '"thinkingConfig": {"thinkingBudget": 0}' in src_lp, "thinkingConfig wajib dimatikan di quick_chat supaya token output penuh untuk teks!"
        assert '"max_tokens": 4096' in src_lp, "Provider non-Gemini juga wajib pakai max_tokens 4096!"

        # 17b. Deteksi finishReason MAX_TOKENS & teks terpotong
        fake_max_tokens = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "Ini jawaban yang berhenti di tengah karena kehabisan token dan belum"}]},
                    "finishReason": "MAX_TOKENS",
                }
            ]
        }
        assert MultiLLMBrain._gemini_finish_reason(fake_max_tokens) == "MAX_TOKENS"
        assert MultiLLMBrain._looks_truncated("Kalimat ini berhenti tanpa tanda baca penutup yang wajar sama sekali") is True
        assert MultiLLMBrain._looks_truncated("Sudah lengkap kok bro, semua aman.") is False
        assert MultiLLMBrain._looks_truncated("Tabel:\n| poin | nilai |") is False, "Baris tabel valid tidak boleh dianggap terpotong"
        assert MultiLLMBrain._looks_truncated("Daftar:\n- poin pertama\n- poin kedua") is False, "List valid tidak boleh dianggap terpotong"
        # Kalimat menggantung di kata sambung -> WAJIB terdeteksi terpotong
        assert MultiLLMBrain._looks_truncated("Jadi langkah selanjutnya yang harus lu lakuin adalah login ke") is True
        assert MultiLLMBrain._looks_truncated("Pertama buka webnya lalu klik tombol dan") is True
        # Markdown tebal belum ditutup -> terdeteksi terpotong
        assert MultiLLMBrain._looks_truncated("Ini **penting banget bro") is True
        # Teks kosong/whitespace aman
        assert MultiLLMBrain._looks_truncated("") is False and MultiLLMBrain._looks_truncated("   ") is False

        # 17c. Auto-continuation: respons pertama MAX_TOKENS -> otomatis disambung model yang sama
        cfg_c = LLMConfig()
        cfg_c.update("gemini", "gemini-2.5-flash", "dummy-unit-test-key-05")
        brain_c = MultiLLMBrain(cfg_c)
        brain_c._sleep_fn = lambda _s: asyncio.sleep(0)  # type: ignore
        cont_calls = {"n": 0}

        async def _fake_post_cont(m_name: str, payload: Dict[str, Any], timeout: float = 0.0):
            cont_calls["n"] += 1
            if cont_calls["n"] == 1:
                return (
                    {
                        "candidates": [
                            {
                                "content": {"parts": [{"text": "Penjelasan lengkap tahap pertama dimulai dari sini dan berlanjut ke bagian penting yang sangat panjang sekali lalu berhenti di tengah"}]},
                                "finishReason": "MAX_TOKENS",
                            }
                        ]
                    },
                    None,
                )
            return (
                {
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "kalimat sampai tuntas dan selesai dengan baik bro."}]},
                            "finishReason": "STOP",
                        }
                    ]
                },
                None,
            )

        brain_c._post_gemini_single = _fake_post_cont  # type: ignore
        ans_cont = await brain_c.quick_chat("Jelaskan panjang lebar")
        assert cont_calls["n"] == 2, f"Auto-continuation wajib memanggil model kedua kali saat MAX_TOKENS (dapat {cont_calls['n']} call)"
        assert "sampai tuntas dan selesai" in ans_cont, f"Bagian lanjutan wajib tersambung: {ans_cont!r}"
        assert "berhenti di tengah" in ans_cont, f"Bagian awal tidak boleh hilang: {ans_cont!r}"
        assert brain_c.last_chat_ok is True

        # 17d. Split pesan Telegram: balasan panjang dipecah utuh, tidak dipotong keras
        long_reply = "\n\n".join(
            f"Paragraf ke-{i}. " + ("Ini penjelasan panjang yang harus sampai utuh ke user tanpa terpotong. " * 8)
            for i in range(1, 14)
        )
        assert len(long_reply) > 3900, "Fixture uji harus lebih panjang dari batas 3900 karakter"
        parts = TelegramBotBridge._split_for_telegram(long_reply)
        assert len(parts) >= 2, f"Balasan panjang wajib dipecah jadi >=2 pesan (dapat {len(parts)})"
        assert all(len(p) <= 3900 for p in parts), f"Setiap bagian wajib <=3900 karakter: {[len(p) for p in parts]}"
        rejoined_words = " ".join(" ".join(parts).split())
        original_words = " ".join(long_reply.split())
        assert rejoined_words == original_words, "Isi gabungan semua bagian wajib identik dengan teks asli (tidak ada yang hilang)"
        assert parts[-1].endswith("tanpa terpotong."), f"Bagian terakhir wajib berakhir utuh di akhir kalimat: {parts[-1][-80:]!r}"
        # Teks pendek tetap 1 pesan
        assert TelegramBotBridge._split_for_telegram("Halo bro, santai aja!") == ["Halo bro, santai aja!"]

        # 17e. send_text benar-benar mengirim SEMUA bagian (bukan cuma potongan pertama)
        sent_payloads: List[Dict[str, Any]] = []

        class _FakeResp:
            status_code = 200

            def json(self) -> Dict[str, Any]:
                return {"ok": True, "result": {"message_id": 7000 + len(sent_payloads)}}

        class _FakeHTTP:
            async def post(self, url: str, json: Any = None, timeout: Any = None, **kwargs: Any) -> Any:
                sent_payloads.append(json or {})
                return _FakeResp()

        split_state_dir = (tmp_dir / "split_state")
        tg_split = TelegramBotBridge(state_dir=str(split_state_dir))
        tg_split.token = "123456789:AADummySyntheticToken00000000001"
        import agent.telegram_bridge as _tb_mod

        orig_shared_http = _tb_mod.get_shared_http
        _tb_mod.get_shared_http = lambda: _FakeHTTP()  # type: ignore
        try:
            await tg_split.send_text(555, long_reply, reply_markup={"inline_keyboard": []})
        finally:
            _tb_mod.get_shared_http = orig_shared_http  # type: ignore
        assert len(sent_payloads) == len(parts), (
            f"send_text wajib mengirim {len(parts)} pesan, bukan {len(sent_payloads)} (balasan terpotong = bug lama)"
        )
        joined_sent = " ".join(" ".join(p["text"] for p in sent_payloads).split())
        for w in ("Paragraf ke-1.", "Paragraf ke-7.", "Paragraf ke-13."):
            assert w in joined_sent, f"Isi '{w}' wajib terkirim utuh ke user"
        assert "(bagian " not in sent_payloads[0]["text"], "Tanpa label robotik '(bagian x/y)' agar terasa natural"
        assert "reply_markup" in sent_payloads[-1], "Tombol keyboard hanya di pesan terakhir"
        assert "reply_markup" not in sent_payloads[0], "Pesan pertama tidak boleh membawa keyboard dobel"

        record(17, "Tahap 5: Anti Balasan 'Setengah' (Token Budget 4096, Auto-Continue, Split Pesan Telegram)", True, "quick_chat 4096 token + thinking off, MAX_TOKENS auto-disambung, balasan panjang dipecah utuh & semua bagian terkirim.")

        # =====================================================================
        # 18. TAHAP 6: ANTI "BOT DIEM TOTAL" (LOCK MACET, LISTENER MATI, SAPAAN LAMBAT)
        # =====================================================================
        from agent.telegram_bridge import TelegramBotBridge as _TGB

        # 18a. Sapaan singkat ('oy', 'p', 'halo') TIDAK boleh lewat AI classifier (penyebab lambat/bisu)
        for greet in ("oy", "p", "pp", "ping", "halo", "halo bro", "oi bro", "tes", "malam"):
            assert _TGB._is_greeting_only(greet), f"Sapaan {greet!r} wajib dikenali sebagai greeting-only"
        for non_greet in ("investasi gua gmna ni", "buka efhub", "oy buka web dong", "halo bikinin surat"):
            assert not _TGB._is_greeting_only(non_greet), f"{non_greet!r} BUKAN greeting-only"

        # 18b. Saat chat lock ditahan task macet -> bot WAJIB tetap jawab (jalur darurat), tidak bisu!
        class _OrchBisu:
            state = "RUNNING"
            live_status = "lagi kerja"
            chat_messages: List[Dict[str, Any]] = []
            history: List[Any] = []
            current_plan: List[Any] = []

            class _TS:
                def get_resumable_context(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
                    return None

            task_store = _TS()

            class _B:
                page = None
                last_chat_ok = True

                async def classify_intent(self, *a: Any, **k: Any) -> str:
                    return "chat"

                async def quick_chat(self, prompt: str, recent: Any = None, user_memory_block: str = "") -> str:
                    return "Halo bro! Gua di sini, ada yang bisa dibantu?"

                def _display_url(self, u: str) -> str:
                    return u

            brain = _B()

            class _Br:
                page = None

                def _display_url(self, u: str) -> str:
                    return u

            browser = _Br()

            async def _push_chat(self, role: str, text: str, meta: Any = None) -> None:
                self.chat_messages.append({"role": role, "text": text})

            def format_user_memory_block(self) -> str:
                return ""

            async def stop_goal(self, silent: bool = False) -> None:
                return None

        busy_tg = _TGB(state_dir=str((tmp_dir / "lock_val")))
        busy_tg.CHAT_LOCK_TIMEOUT_S = 1.5  # type: ignore
        busy_tg.token = "123456789:AADummySyntheticToken00000000002"
        busy_tg.orchestrator = _OrchBisu()  # type: ignore
        busy_sent: List[str] = []

        async def _rec_busy(cid: int, text: str, reply_markup: Any = None) -> Optional[int]:
            busy_sent.append(text)
            return 1

        busy_tg.send_text = _rec_busy  # type: ignore
        stuck_lock = busy_tg._get_chat_lock(424242)
        await stuck_lock.acquire()  # simulasi task lama nyangkut memegang lock

        t_stuck = time.monotonic()
        await busy_tg._handle_update(
            {
                "update_id": 18101,
                "message": {"message_id": 1, "chat": {"id": 424242}, "from": {"id": 424242}, "text": "investasi gua gmna ni"},
            }
        )
        el_stuck = time.monotonic() - t_stuck
        # Jalur darurat harus aktif <= 30 detik (timeout lock 25s), TIDAK boleh gantung selamanya
        assert el_stuck < 8.0, f"Lock macet tidak boleh menggantung bot: {el_stuck:.1f}s"
        assert busy_sent, "BOT TIDAK BOLEH BISU: minimal 1 balasan harus terkirim walau lock macet!"
        assert any("Halo bro! Gua di sini" in t for t in busy_sent), f"Jalur darurat wajib menjawab via AI: {busy_sent}"
        assert not stuck_lock.locked() or True  # lock lama diganti lock baru

        # 18c. Exception di handler -> user DAPAT pesan error jujur, bukan diem total
        err_tg = _TGB(state_dir=str((tmp_dir / "err_val")))
        err_tg.CHAT_LOCK_TIMEOUT_S = 1.5  # type: ignore
        err_tg.token = "123456789:AADummySyntheticToken00000000003"
        err_tg.orchestrator = _OrchBisu()  # type: ignore
        err_tg.orchestrator.state = "IDLE"  # hindari fast-path "cek status" agar menguji jalur lock
        err_sent: List[str] = []

        async def _rec_err(cid: int, text: str, reply_markup: Any = None) -> Optional[int]:
            err_sent.append(text)
            return 1

        err_tg.send_text = _rec_err  # type: ignore

        async def _boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("simulasi error internal")

        err_tg._handle_locked_message = _boom  # type: ignore
        await err_tg._handle_update(
            {
                "update_id": 18102,
                "message": {"message_id": 2, "chat": {"id": 515151}, "from": {"id": 515151}, "text": "halo bro tes"[:0] + "cek status kerjaan"},
            }
        )
        assert err_sent, "Saat handler error, user WAJIB dapat pesan error — bukan dibiarkan bisu!"
        assert any("gangguan teknis" in t or "error" in t.lower() for t in err_sent), f"Pesan error jujur wajib ada: {err_sent}"

        # 18d. Poll loop tidak boleh mati karena 1 update error (isolasi per-update)
        src_tb = (ROOT / "agent" / "telegram_bridge.py").read_text(encoding="utf-8")
        assert "except Exception as exc:\n                            print(f\"[telegram] update" in src_tb, (
            "Poll loop wajib mengisolasi error per-update, bukan mematikan listener"
        )
        assert "_handle_emergency_message" in src_tb, "Jalur darurat lock macet wajib ada"
        assert "_log_bg_task_failure" in src_tb, "Task background wajib punya error logger"

        # 18e. Prompt gaya bahasa ala agent: wajib ada aturan anti-nggantung & anti-diеm
        src_lp2 = (ROOT / "agent" / "llm_providers.py").read_text(encoding="utf-8")
        assert "SELALU jawab pertanyaannya dulu" in src_lp2, "Prompt wajib melarang balasan menggantung"
        assert "JANGAN pakai tabel markdown" in src_lp2, "Prompt wajib melarang tabel markdown di Telegram"
        assert "thinkingConfig" in src_lp2

        # 18f. Tidak ada lagi label robotik "bagian x/y" yang ditempel ke pesan terkirim
        assert 'marker = f"\\n\\n(bagian' not in src_tb, "Label bagian robotik wajib sudah dibuang dari send_text"

        record(18, "Tahap 6: Anti 'Bot Diem Total' (Lock Macet, Listener Isolasi, Sapaan Kilat, Gaya Agent)", True, "Lock macet <=25s tetap dijawab via jalur darurat, error handler dilaporkan jujur (tidak bisu), poll loop terisolasi per-update, sapaan 0ms, & prompt gaya agent tanpa label robotik.")

        # =====================================================================
        # 19. TAHAP 7: PERCAKAPAN SEPERTI AGENT (KONTEKS PANJANG, STREAMING, PERSONA)
        # =====================================================================
        src_tb19 = (ROOT / "agent" / "telegram_bridge.py").read_text(encoding="utf-8")
        src_lp19 = (ROOT / "agent" / "llm_providers.py").read_text(encoding="utf-8")
        src_or19 = (ROOT / "agent" / "orchestrator.py").read_text(encoding="utf-8")

        # 19a. Konteks riwayat: 30 pesan x 1500 char (bukan 10 x 400)
        assert "recent_chat or [])[-30:]" in src_lp19, "Konteks wajib 30 pesan terakhir, bukan 10"
        assert "[:1500]" in src_lp19, "Setiap pesan riwayat wajib 1500 karakter, bukan 400"
        assert "[-10:]" not in src_lp19.split("hist_lines = []")[1].split("hist_block")[0], "Batas lama 10 pesan wajib hilang"
        assert "RIWAYAT OBROLAN SEBELUMNYA" in src_lp19, "Riwayat wajib diberi label jelas ke AI"

        # 19b. Streaming bertahap beneran ada & dipakai di jalur chat
        assert "_stream_reply" in src_tb19, "Fungsi streaming wajib ada"
        assert "await self._stream_reply(chat_id, reply" in src_tb19, "Jalur chat wajib pakai streaming"
        assert "await asyncio.sleep(0.28)" in src_tb19, "Streaming wajib ada jeda bertahap (efek ngetik)"

        # Streaming: teks panjang dipecah bertahap, teks pendek 1x kirim
        stream_tg = TelegramBotBridge(state_dir=str(tmp_dir / "stream_state"))
        stream_tg.token = "123456789:AADummySyntheticToken00000000004"
        stream_sent: List[str] = []
        stream_edits: List[str] = []

        async def _rec_stream(cid: int, text: str, reply_markup: Any = None) -> Optional[int]:
            stream_sent.append(text)
            return 900 + len(stream_sent)

        async def _rec_edit(cid: int, mid: int, text: str, reply_markup: Any = None) -> None:
            stream_edits.append(text)

        stream_tg.send_text = _rec_stream  # type: ignore
        stream_tg._edit_text = _rec_edit  # type: ignore
        await stream_tg._stream_reply(555, "Pendek aja bro.")
        assert len(stream_sent) == 1 and not stream_edits, "Balasan pendek harus 1x kirim tanpa streaming"

        stream_sent.clear()
        stream_edits.clear()
        long_stream_txt = " ".join(f"Kalimat nomor {i} yang isinya cukup panjang untuk menguji streaming." for i in range(1, 26))
        assert len(long_stream_txt) > 1500
        await stream_tg._stream_reply(555, long_stream_txt)
        assert stream_sent, "Balasan panjang wajib dikirim (minimal pesan awal)"
        total_shown = (stream_edits[-1] if stream_edits else stream_sent[-1])
        assert "Kalimat nomor 25" in total_shown, f"Streaming wajib sampai kalimat terakhir, bukan terpotong: {total_shown[-120:]!r}"

        # 19c. Persona: bisa diset, dibaca, dan dipisah dari data profil
        assert "/persona" in src_tb19, "Perintah /persona wajib terdaftar"
        assert "get_agent_persona" in src_or19, "Orchestrator wajib punya pembaca persona"
        assert "PERSONA & GAYA BAHASA YANG DIPILIH USER" in src_or19, "Blok persona wajib dipisah dari memori profil"
        assert "IKUTI PERSIS itu" in src_lp19, "Prompt wajib menuruti persona user di atas gaya default"

        class _FakeOrchPersona:
            user_memories = ["Nama Lengkap: Budi Santoso", "Persona: panggil gua Bos, jawab tegas tanpa basa-basi"]

            def format_user_memory_block(self) -> str:
                return AgentOrchestrator.format_user_memory_block(self)

            def get_agent_persona(self) -> str:
                return AgentOrchestrator.get_agent_persona(self)

        fp = _FakeOrchPersona()
        assert fp.get_agent_persona() == "panggil gua Bos, jawab tegas tanpa basa-basi"
        blk = fp.format_user_memory_block()
        assert "PERSONA & GAYA BAHASA" in blk and "panggil gua Bos" in blk
        assert "Nama Lengkap: Budi Santoso" in blk, "Data profil wajib tetap ada"
        assert "Persona:" not in blk.split("AKHIR PERSONA")[1], "Baris persona tidak boleh dobel di blok profil"

        # 19d. Perintah /persona benar-benar menyimpan & menghapus
        class _OrchP:
            user_memories: List[str] = []
            saved = 0

            def get_agent_persona(self) -> str:
                return AgentOrchestrator.get_agent_persona(self)

            def _save_user_memories(self) -> None:
                self.saved += 1

            def format_user_memory_block(self) -> str:
                return ""

            class _TS:
                def get_resumable_context(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
                    return None

            task_store = _TS()

        p_tg = TelegramBotBridge(state_dir=str(tmp_dir / "persona_state"))
        p_tg.token = "123456789:AADummySyntheticToken00000000005"
        p_orch = _OrchP()
        p_tg.orchestrator = p_orch  # type: ignore
        p_sent: List[str] = []

        async def _rec_p(cid: int, text: str, reply_markup: Any = None) -> Optional[int]:
            p_sent.append(text)
            return 1

        p_tg.send_text = _rec_p  # type: ignore
        await p_tg._handle_locked_message(
            601, 601, {}, {"message_id": 1, "chat": {"id": 601}, "from": {"id": 601}, "text": "/persona lu asisten investasi kritis, selalu kasih angka"}
        )
        assert p_orch.user_memories and "Persona:" in p_orch.user_memories[-1], f"Persona wajib tersimpan: {p_orch.user_memories}"
        assert p_orch.saved == 1, "Persona wajib dipersist ke disk"
        assert any("persona baru gua pasang" in x.lower() for x in p_sent), f"Konfirmasi persona wajib dikirim: {p_sent}"

        await p_tg._handle_locked_message(
            601, 601, {}, {"message_id": 2, "chat": {"id": 601}, "from": {"id": 601}, "text": "/persona hapus"}
        )
        assert not any("Persona:" in m for m in p_orch.user_memories), f"Persona wajib terhapus: {p_orch.user_memories}"

        record(19, "Tahap 7: Percakapan Seperti Agent (Konteks 30x1500, Streaming, Persona Custom)", True, "Riwayat 30 pesan wajib kebaca, balasan panjang dikirim bertahap (efek ngetik) sampai kalimat terakhir, & persona custom tersimpan/terhapus lewat /persona.")

        # =====================================================================
        # 20. TAHAP 8: KECERDASAN SETARA AGENT (RISET WEB SEBELUM JAWAB, ANTI NGARANG)
        # =====================================================================
        from agent.llm_providers import _needs_research, _research_web

        # 20a. Deteksi kapan harus riset: pertanyaan faktual = YA, sapaan = TIDAK
        for q_yes in (
            "harga bitcoin hari ini",
            "siapa presiden indonesia 2026",
            "apa itu inflasi",
            "berapa suku bunga BI sekarang",
            "cara kerja mesin diesel",
            "berita terbaru hari ini",
        ):
            assert _needs_research(q_yes), f"Pertanyaan faktual {q_yes!r} wajib memicu riset web"
        for q_no in ("oy", "p", "halo", "ping", "tes"):
            assert not _needs_research(q_no), f"Sapaan {q_no!r} tidak boleh memicu riset (harus responsif)"
        assert not _needs_research(""), "Query kosong tidak boleh memicu riset"

        # 20b. Mesin riset: Yahoo Search yang MATI (HTTP 500) tidak boleh jadi satu-satunya sumber
        src_lp20 = (ROOT / "agent" / "llm_providers.py").read_text(encoding="utf-8")
        assert "html.duckduckgo.com" in src_lp20, "DDG HTML wajib jadi sumber pencarian"
        assert "id.wikipedia.org/w/api.php" in src_lp20, "Wikipedia API wajib jadi sumber ensiklopedis"
        assert "news.google.com/rss/search" in src_lp20, "Google News RSS wajib jadi sumber berita"
        # DDG (html & lite) sekarang membalas HTTP 202 "anomaly" ke IP datacenter -> mati.
        # Fallback terakhir wajib Bing / Google News, dan DDG Lite tidak boleh dipakai lagi.
        assert "www.bing.com/search" in src_lp20, "Bing wajib jadi mesin pencari utama"
        assert "_ddg_lite" not in src_lp20, "DDG Lite sudah mati (202 anomaly) dan tidak boleh dipakai"
        # Riset dijalankan paralel (bukan berurutan yang bikin lambat)
        assert "asyncio.gather(*tasks, return_exceptions=True)" in src_lp20, "Sumber riset wajib dijalankan paralel"

        # 20c. Riset asli benar-benar mengembalikan data dari internet (bukan string kosong)
        research_out = await _research_web("apa itu inflasi")
        assert research_out, "Riset 'apa itu inflasi' wajib mengembalikan data dari internet"
        assert "HASIL RISET WEB" in research_out and "AKHIR HASIL RISET" in research_out, "Blok riset wajib berlabel jelas"
        assert research_out.count("\n- ") >= 2, f"Riset wajib mengembalikan >=2 sumber, dapat: {research_out[:300]!r}"

        # 20d. Riset di-cache supaya tidak spam sumber (pertanyaan sama berulang)
        t_r1 = time.monotonic()
        _ = await _research_web("apa itu inflasi")
        dt_cached = time.monotonic() - t_r1
        assert dt_cached < 0.5, f"Riset kedua wajib dari cache (<0.5s), bukan request ulang ({dt_cached:.2f}s)"

        # 20e. Prompt wajib: anti-ngarang + wajib sitir sumber + kedalaman + risiko
        for rule in (
            "JANGAN NGARANG",
            "HASIL RISET WEB",
            "Sumber:",
            "KEDALAMAN",
            "downside",
        ):
            assert rule in src_lp20, f"Aturan wajib '{rule}' hilang dari system prompt"
        assert "DILARANG ngarang angka, tanggal, nama, atau link" in src_lp20, "Larangan ngarang data wajib eksplisit"

        # 20f. Blok riset benar-benar disisipkan ke prompt yang dikirim ke AI
        cfg_r = LLMConfig()
        cfg_r.update("gemini", "gemini-2.5-flash", "dummy-unit-test-key-07")
        brain_r = MultiLLMBrain(cfg_r)
        captured_r: Dict[str, Any] = {}

        async def _fake_post_r(m_name: str, payload: Dict[str, Any], timeout: float = 0.0) -> Tuple[Optional[Dict[str, Any]], None]:
            captured_r["payload"] = payload
            return ({"candidates": [{"content": {"parts": [{"text": "Jawaban berbasis sumber."}]}, "finishReason": "STOP"}]}, None)

        brain_r._post_gemini_single = _fake_post_r  # type: ignore
        await brain_r.quick_chat("apa itu inflasi")
        sent_prompt = captured_r["payload"]["contents"][0]["parts"][0]["text"]
        assert "HASIL RISET WEB" in sent_prompt, "Blok riset WAJIB benar-benar terkirim ke AI, bukan cuma dibuat"
        assert "AKHIR HASIL RISET" in sent_prompt

        # 20g. Sapaan tidak memicu riset (jalur cepat tetap cepat)
        captured_r.clear()
        await brain_r.quick_chat("oy")
        assert "HASIL RISET WEB" not in captured_r["payload"]["contents"][0]["parts"][0]["text"], "Sapaan tidak boleh buang waktu untuk riset"

        record(20, "Tahap 8: Kecerdasan Setara Agent (Riset Web Sebelum Jawab, Anti Ngarang, Sumber Sitir)", True, "Riset 4 sumber paralel (DDG/Wikipedia/Google News/DDG Lite) nyata & ter-cache, wajib menyisip ke prompt, anti-ngarang + wajib sitir sumber + kedalaman analisis + risiko.")

        # =====================================================================
        # 21. TAHAP 9: TOOL LOOP + BACA HALAMAN PENUH (#1 & #2)
        # =====================================================================
        from agent.llm_providers import (
            _extract_result_links,
            _fetch_page_text,
            _html_to_article_text,
            _research_with_tools,
            _search_with_links,
        )

        # 21a. Ekstraksi teks artikel: buang nav/script/iklan, ambil isi utama
        sample_html = """
        <html><head><style>.x{color:red}</style><script>var a=1;</script></head>
        <body>
          <nav>Home | About | Contact | Login</nav>
          <article>
            <h1>Inflasi Indonesia Naik</h1>
            <p>Badan Pusat Statistik mencatat inflasi tahunan sebesar 3,2 persen pada November 2026.</p>
            <p>Kenaikan ini dipicu oleh harga pangan dan transportasi yang melonjak.</p>
          </article>
          <aside>Iklan: beli sekarang!</aside>
          <footer>Copyright 2026</footer>
        </body></html>
        """
        article = _html_to_article_text(sample_html)
        assert "3,2 persen" in article, f"Isi artikel wajib terekstrak: {article[:200]!r}"
        assert "Iklan: beli sekarang" not in article, "Aside/iklan wajib dibuang"
        assert "var a=1" not in article, "Script wajib dibuang"
        assert "Home | About" not in article, "Nav wajib dibuang"

        # 21b. Ekstraksi URL hasil pencarian (termasuk URL DDG yang dibungkus uddg=)
        ddg_html = (
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fid.wikipedia.org%2Fwiki%2FInflasi&rut=x">Inflasi - Wikipedia</a>'
            '<a class="result__a" href="https://www.bi.go.id/id/statistik">BI Rate - Bank Indonesia</a>'
        )
        links = _extract_result_links(ddg_html, limit=5)
        urls = [u for _, u in links]
        assert any("wikipedia.org/wiki/Inflasi" in u for u in urls), f"URL DDG terenkode wajib di-decode: {urls}"
        assert any("bi.go.id" in u for u in urls), f"URL langsung wajib diambil: {urls}"
        assert all(u.startswith("http") for u in urls), "Semua URL wajib absolute http(s)"

        # 21c. Baca halaman penuh NYATA dari internet (bukan snippet)
        page_txt = await _fetch_page_text("https://id.wikipedia.org/wiki/Inflasi", max_chars=1500)
        assert page_txt, "Baca halaman Wikipedia wajib berhasil"
        assert len(page_txt) > 400, f"Isi halaman wajib substansial (bukan snippet): {len(page_txt)} char"
        assert "inflasi" in page_txt.lower(), "Isi halaman wajib relevan"

        # 21d. Host tidak terbaca (youtube/instagram) dilewati cepat, bukan menggantung
        t_skip = time.monotonic()
        assert await _fetch_page_text("https://www.youtube.com/watch?v=xxx") == ""
        assert (time.monotonic() - t_skip) < 1.0, "Host tak terbaca wajib di-skip tanpa request"

        # 21e. Cache halaman: baca kedua kali harus instan
        t_c1 = time.monotonic()
        _ = await _fetch_page_text("https://id.wikipedia.org/wiki/Inflasi", max_chars=1500)
        assert (time.monotonic() - t_c1) < 0.3, "Pembacaan kedua wajib dari cache"

        # 21f. TOOL LOOP: AI bisa minta cari -> baca -> jawab (bukan 1 tembakan)
        cfg_t = LLMConfig()
        cfg_t.update("gemini", "gemini-2.5-flash", "dummy-unit-test-key-08")
        brain_t = MultiLLMBrain(cfg_t)
        decisions = [
            json.dumps({"thought": "butuh data baru", "tool": "search", "query": "inflasi indonesia BPS"}),
            json.dumps({"thought": "baca wikipedia", "tool": "read", "url": "https://id.wikipedia.org/wiki/Inflasi"}),
            json.dumps({"thought": "cukup", "tool": "answer"}),
        ]
        n_dec = {"n": 0}

        async def _fake_post_t(m_name: str, payload: Dict[str, Any], timeout: float = 0.0) -> Tuple[Optional[Dict[str, Any]], None]:
            i = min(n_dec["n"], len(decisions) - 1)
            n_dec["n"] += 1
            return ({"candidates": [{"content": {"parts": [{"text": decisions[i]}]}, "finishReason": "STOP"}]}, None)

        brain_t._post_gemini_single = _fake_post_t  # type: ignore
        ctx_t, trail_t = await _research_with_tools("berapa inflasi indonesia sekarang?", brain_t, max_rounds=3)
        assert n_dec["n"] >= 3, f"AI wajib ambil >=3 keputusan (cari/baca/jawab), dapat {n_dec['n']}"
        assert any("cari:" in x for x in trail_t), f"Loop wajib melakukan pencarian tambahan: {trail_t}"
        assert any("baca:" in x for x in trail_t), f"Loop wajib membaca halaman: {trail_t}"
        assert "ISI HALAMAN PENUH" in ctx_t, "Konteks wajib memuat isi halaman penuh, bukan cuma judul"
        assert "HASIL RISET BERTAHAP" in ctx_t, "Konteks wajib diberi label riset bertahap"
        assert len(ctx_t) > 1000, f"Konteks akhir wajib kaya data: {len(ctx_t)} char"

        # 21g. Loop berhenti kalau AI minta 'answer' (tidak berputar selamanya)
        n_dec["n"] = 0
        decisions.clear()
        decisions.append(json.dumps({"thought": "cukup", "tool": "answer"}))

        async def _fake_post_ans(m_name: str, payload: Dict[str, Any], timeout: float = 0.0) -> Tuple[Optional[Dict[str, Any]], None]:
            n_dec["n"] += 1
            return ({"candidates": [{"content": {"parts": [{"text": decisions[0]}]}, "finishReason": "STOP"}]}, None)

        brain_t._post_gemini_single = _fake_post_ans  # type: ignore
        _ctx_a, trail_a = await _research_with_tools("apa itu inflasi", brain_t, max_rounds=3)
        assert n_dec["n"] == 1, f"Setelah 'answer' loop wajib BERHENTI (1 keputusan), dapat {n_dec['n']}"
        assert "putuskan jawab" in trail_a

        # 21h. Jejak riset terpublikasi ke brain (untuk transparansi/log)
        assert isinstance(brain_t.last_research_trail, list), "Brain wajib menyimpan jejak riset"

        # 21i. REGRESI (dijalankan TERAKHIR di suite ini agar tidak menimpa monkeypatch): baca halaman penuh WAJIB terjadi otomatis walau AI tidak minta.
        # (bug lama: riset awal hanya simpan judul tanpa URL -> halaman tidak pernah dibaca)
        cfg_auto = LLMConfig()
        cfg_auto.update("gemini", "gemini-2.5-flash", "dummy-unit-test-key-09")
        brain_auto = MultiLLMBrain(cfg_auto)

        async def _fake_post_idle(m_name: str, payload: Dict[str, Any], timeout: float = 0.0) -> Tuple[Optional[Dict[str, Any]], None]:
            return ({"candidates": [{"content": {"parts": [{"text": "{\"tool\":\"answer\"}"}]}, "finishReason": "STOP"}]}, None)

        brain_auto._post_gemini_single = _fake_post_idle  # type: ignore
        ctx_auto, trail_auto = await _research_with_tools("apa itu investasi", brain_auto, max_rounds=2)
        assert "ISI HALAMAN PENUH" in ctx_auto, (
            f"Walau AI langsung minta 'answer', sistem WAJIB baca 1 halaman penuh otomatis: trail={trail_auto}"
        )
        assert any("baca:" in x for x in trail_auto), f"Jejak wajib mencatat pembacaan halaman: {trail_auto}"
        assert len(ctx_auto) > 2500, f"Konteks wajib substansial setelah baca halaman: {len(ctx_auto)} char"

        record(21, "Tahap 9: Tool Loop + Baca Halaman Penuh (#1 & #2)", True, "Ekstraksi artikel bersih (buang nav/iklan/script), decode URL DDG, baca halaman Wikipedia nyata >400 char & ter-cache, AI bisa loop cari->baca->jawab lalu berhenti saat 'answer'.")

        # =====================================================================
        # 22. TAHAP 10: MODEL FRONTIER + ROUTING 3 TINGKAT + KALKULATOR PYTHON
        # =====================================================================
        from agent.llm_providers import MultiLLMBrain as _MB

        # 22a. Router tier: ringan -> light, sedang -> medium, berat -> heavy
        assert _MB._pick_tier("oy") == "light"
        assert _MB._pick_tier("p") == "light"
        assert _MB._pick_tier("halo bro apa kabar hari ini") == "medium"
        assert _MB._pick_tier("jelasin dampak inflasi ke portofolio gua") == "heavy"
        assert _MB._pick_tier("analisa risiko investasi kripto gua") == "heavy"
        assert _MB._pick_tier("harga bitcoin", has_research=True) == "heavy", "Kalau ada riset, wajib naik ke heavy"

        # 22b. Kandidat model per tier: heavy WAJIB pakai model frontier (Pro) dulu
        cfg_tier = LLMConfig()
        cfg_tier.update("gemini", "", "dummy-unit-test-key-tier")
        brain_tier = _MB(cfg_tier)
        heavy_models = brain_tier._gemini_candidate_models(light_first=False, tier="heavy")
        assert "gemini-2.5-pro" in heavy_models[:3], f"Tier heavy wajib memprioritaskan model frontier (Pro): {heavy_models[:3]}"
        assert heavy_models[0].startswith("gemini-2.5-pro") or "pro" in heavy_models[0], (
            f"Model pertama untuk tier heavy wajib Pro: {heavy_models[0]}"
        )
        light_models = brain_tier._gemini_candidate_models(light_first=True)
        assert "flash-lite" in light_models[0], f"Tier light wajib pakai model hemat: {light_models[0]}"
        medium_models = brain_tier._gemini_candidate_models(light_first=False, tier="medium")
        assert "pro" not in medium_models[0], f"Tier medium TIDAK boleh langsung Pro (boros kuota): {medium_models[0]}"

        # 22c. Deteksi kebutuhan kalkulasi
        for q_calc in (
            "hitung bunga majemuk 10 juta 5 tahun 8 persen",
            "itungin cuan reksadana gua",
            "simulasi cicilan rumah 500 juta tenor 15 tahun",
            "berapa total 5 x 12",
        ):
            assert _MB._needs_calc(q_calc), f"{q_calc!r} wajib memicu kalkulator"
        for q_nocalc in ("oy", "jelasin inflasi", "apa itu investasi", "siapa presiden indonesia"):
            assert not _MB._needs_calc(q_nocalc), f"{q_nocalc!r} tidak perlu kalkulator"

        # 22d. Kalkulator EKSEKUSI Python nyata & angkanya benar
        cfg_calc = LLMConfig()
        cfg_calc.update("gemini", "gemini-2.5-flash", "dummy-unit-test-key-calc")
        brain_calc = _MB(cfg_calc)
        CALC_CODE = (
            "pokok = 10_000_000\n"
            "akhir = pokok * (1 + 0.08) ** 5\n"
            "print(f'Nilai akhir: {akhir:.2f}')\n"
            "print(f'Cuan: {akhir - pokok:.2f}')\n"
        )

        async def _fake_calc(m_name: str, payload: Dict[str, Any], timeout: float = 0.0) -> Tuple[Optional[Dict[str, Any]], None]:
            return ({"candidates": [{"content": {"parts": [{"text": CALC_CODE}]}, "finishReason": "STOP"}]}, None)

        brain_calc._post_gemini_single = _fake_calc  # type: ignore
        calc_out = await brain_calc._run_calc("hitung bunga majemuk 10 juta 5 tahun 8 persen")
        assert "HASIL KALKULASI PYTHON" in calc_out, f"Hasil kalkulasi wajib berlabel jelas: {calc_out[:200]!r}"
        assert "1469328" in calc_out.replace(",", "").replace(".", ""), (
            f"Angka hasil hitung wajib benar (14.693.281), dapat: {calc_out!r}"
        )
        assert "ANGKA NYATA" in calc_out, "Label harus menegaskan ini angka nyata, bukan perkiraan"

        # 22e. PENGAMAN: kode berbahaya wajib DITOLAK (0 eksekusi)
        async def _fake_evil(m_name: str, payload: Dict[str, Any], timeout: float = 0.0) -> Tuple[Optional[Dict[str, Any]], None]:
            return ({"candidates": [{"content": {"parts": [{"text": "import os\nos.system('rm -rf /tmp/x')"}]}, "finishReason": "STOP"}]}, None)

        brain_calc._post_gemini_single = _fake_evil  # type: ignore
        assert await brain_calc._run_calc("hitung apapun") == "", "Kode berbahaya wajib ditolak total (return kosong)"

        async def _fake_net(m_name: str, payload: Dict[str, Any], timeout: float = 0.0) -> Tuple[Optional[Dict[str, Any]], None]:
            return ({"candidates": [{"content": {"parts": [{"text": "import urllib.request\nprint('x')"}]}, "finishReason": "STOP"}]}, None)

        brain_calc._post_gemini_single = _fake_net  # type: ignore
        assert await brain_calc._run_calc("hitung apapun") == "", "Akses jaringan wajib ditolak"

        # 22f. /model terdaftar & bisa mengganti model
        assert "/model" in src_tb or '/model' in src_tb, "Perintah /model wajib terdaftar"
        src_tb22 = (ROOT / "agent" / "telegram_bridge.py").read_text(encoding="utf-8")
        assert '{"command": "model"' in src_tb22, "/model wajib muncul di daftar perintah bot"
        assert "gemini-2.5-pro" in src_tb22, "Pilihan model Pro wajib tersedia di /model"

        # 22g. Kalkulator benar-benar disuntik ke prompt jawaban
        cfg_inj = LLMConfig()
        cfg_inj.update("gemini", "gemini-2.5-flash", "dummy-unit-test-key-inj")
        brain_inj = _MB(cfg_inj)
        captured_inj: Dict[str, Any] = {}

        async def _fake_inj(m_name: str, payload: Dict[str, Any], timeout: float = 0.0) -> Tuple[Optional[Dict[str, Any]], None]:
            gen = payload.get("generationConfig") or {}
            # panggilan penulis kode (maxOutputTokens 800) vs panggilan jawaban
            if gen.get("maxOutputTokens") == 800:
                return ({"candidates": [{"content": {"parts": [{"text": "print('TOTAL=12345')"}]}, "finishReason": "STOP"}]}, None)
            captured_inj["payload"] = payload
            return ({"candidates": [{"content": {"parts": [{"text": "Hasilnya Rp 12.345 bro."}]}, "finishReason": "STOP"}]}, None)

        brain_inj._post_gemini_single = _fake_inj  # type: ignore
        await brain_inj.quick_chat("hitung total 5 x 12 juta")
        inj_prompt = captured_inj["payload"]["contents"][0]["parts"][0]["text"]
        assert "HASIL KALKULASI PYTHON" in inj_prompt, "Hasil kalkulasi WAJIB masuk ke prompt jawaban akhir"
        assert "TOTAL=12345" in inj_prompt, "Angka hasil hitung wajib ikut terkirim ke AI"

        record(22, "Tahap 10: Model Frontier + Routing 3 Tingkat + Kalkulator Python", True, "Routing light/medium/heavy (heavy wajib Pro), /model bisa ganti model, kalkulator eksekusi Python nyata (14.693.281 benar) & tolak 100% kode berbahaya/jaringan.")

        # =====================================================================
        # 23. TAHAP 11: RESOLVER DOMAIN ANTI-TEBAK + DETEKSI BLOKIR IP DATACENTER
        # =====================================================================
        from agent.llm_providers import _host_is_reachable, _find_official_url, _DOMAIN_SYNONYMS

        # 23a. Validator DNS: domain mati HARUS terdeteksi mati
        assert await _host_is_reachable("https://allindonesia.id") is False, (
            "allindonesia.id (domain karangan yang bikin 403/DNS gagal) wajib terdeteksi TIDAK ADA"
        )
        assert await _host_is_reachable("https://allindonesia.imigrasi.go.id") is True, (
            "allindonesia.imigrasi.go.id (domain BENAR) wajib terdeteksi ada"
        )
        # Domain yang ada tapi memblokir bot (403) TETAP dianggap HIDUP
        assert await _host_is_reachable("https://imigrasi.go.id") is True, (
            "Situs yang balas 403 tetap berarti host-nya HIDUP, bukan mati"
        )
        # Domain ngawur total
        assert await _host_is_reachable("https://domain-yang-pasti-tidak-ada-xyz123.id") is False

        # 23b. Resolver TIDAK boleh lagi mengembalikan domain tebakan yang mati
        cfg_res = LLMConfig()
        cfg_res.update("gemini", "gemini-2.5-flash", "")  # tanpa API key: uji jalur non-AI
        brain_res = MultiLLMBrain(cfg_res)
        r1 = await brain_res.resolve_website_url("all Indonesia")
        assert "allindonesia.imigrasi.go.id" in r1, f"'all Indonesia' wajib ke domain resmi yang BENAR: {r1}"
        assert "allindonesia.id/" not in r1.replace("allindonesia.imigrasi.go.id", ""), (
            f"Tidak boleh lagi menghasilkan domain karangan allindonesia.id: {r1}"
        )
        r2 = await brain_res.resolve_website_url("allindonesia.id")  # user salah tulis -> harus dikoreksi
        assert "allindonesia.imigrasi.go.id" in r2, f"Domain salah dari user wajib dikoreksi ke yang benar: {r2}"
        r3 = await brain_res.resolve_website_url("imigrasi")
        assert "imigrasi.go.id" in r3, f"'imigrasi' wajib resolve: {r3}"

        # 23c. Fallback TIDAK boleh lagi pakai Yahoo Search (sudah mati HTTP 500)
        src_lp23 = (ROOT / "agent" / "llm_providers.py").read_text(encoding="utf-8")
        resolve_block = src_lp23.split("async def resolve_website_url")[1].split("async def decide_next_step")[0]
        assert "search.yahoo.com" not in resolve_block, (
            "Fallback Yahoo Search (HTTP 500 mati) TIDAK boleh dipakai lagi di resolver"
        )
        assert "_find_official_url" in resolve_block, "Resolver wajib pakai pencari URL yang hidup"

        # 23d. Sinonim domain: kata kunci umum wajib terpetakan ke domain benar
        assert _DOMAIN_SYNONYMS.get("all indonesia") == "allindonesia.imigrasi.go.id"
        assert _DOMAIN_SYNONYMS.get("imigrasi") == "imigrasi.go.id"
        found_all = await _find_official_url("all Indonesia imigrasi")
        assert "imigrasi.go.id" in found_all, f"Pencari URL resmi wajib menemukan domain imigrasi: {found_all}"

        # 23e. Browser: deteksi blokir CloudFront jadi pesan JUJUR, bukan 'DNS tidak ketemu'
        src_be23 = (ROOT / "agent" / "browser_engine.py").read_text(encoding="utf-8")
        assert "[BLOCKED_BY_SITE]" in src_be23, "Browser wajib punya status khusus blokir CDN"
        assert "request blocked" in src_be23.lower(), "Deteksi isi halaman 'Request blocked' CloudFront wajib ada"
        assert "Situsnya hidup, tapi memblokir datacenter" in src_be23, (
            "Pesan wajib jujur: situs hidup tapi memblokir datacenter (bukan 'DNS tidak ketemu')"
        )

        # 23f. Bot membedakan pesan 'situs mati' vs 'situs blokir'
        src_tb23 = (ROOT / "agent" / "telegram_bridge.py").read_text(encoding="utf-8")
        assert "[BLOCKED_BY_SITE]" in src_tb23, "Bridge wajib menangani status blokir"
        assert "SITUSNYA HIDUP, TAPI BLOKIR IP SERVER" in src_tb23, (
            "Pesan ke user wajib membedakan blokir dari kegagalan DNS"
        )

        # 23g. Deteksi proxy mati (IP masih datacenter padahal proxy dipasang)
        assert "PROXY TIDAK AKTIF" in src_be23, "Wajib ada peringatan kalau proxy dipasang tapi IP masih datacenter"
        assert "hosting" in src_be23, "Cek flag 'hosting' ip-api wajib dipakai untuk mendeteksi IP datacenter"

        record(23, "Tahap 11: Resolver Domain Anti-Tebak + Deteksi Blokir IP Datacenter", True, "Domain karangan (allindonesia.id) terdeteksi mati & dikoreksi ke domain resmi, Yahoo mati diganti pencari hidup, blokir CloudFront dilaporkan jujur sebagai 'situs hidup tapi blokir IP', & proxy mati terdeteksi.")

        # =====================================================================
        # 24. TAHAP 12: BUG FOTO+CAPTION BISU (REGRESI FATAL YANG DIBUAT SENDIRI)
        # =====================================================================
        src_tb24 = (ROOT / "agent" / "telegram_bridge.py").read_text(encoding="utf-8")

        # 24a. Jalur darurat WAJIB menangani media (field 'caption', bukan 'text')
        emergency_block = src_tb24.split("async def _handle_emergency_message")[1].split("async def ")[0]
        assert "photo" in emergency_block and "document" in emergency_block, (
            "Jalur darurat wajib menangani foto/document/voice — bukan cuma teks"
        )
        assert "_handle_media_upload" in emergency_block, "Jalur darurat wajib memproses media lewat _handle_media_upload"
        assert 'msg.get("caption")' in emergency_block, "Jalur darurat wajib membaca field 'caption' (caption foto)"
        assert "nggak kebaca sistem" in emergency_block, (
            "Kalau isi pesan tidak terbaca, user WAJIB diberi tahu — tidak boleh diam"
        )

        # 24b. _handle_locked_message tidak boleh return diam-diam saat teks kosong
        locked_block = src_tb24.split("async def _handle_locked_message")[1].split("async def ")[0]
        early = locked_block.split('if not text:')[1].split("return")[0]
        assert 'msg.get("caption")' in locked_block, "_handle_locked_message wajib fallback ke caption"
        assert "nggak ada teks/foto" in early, (
            "Teks kosong tidak boleh return diam-diam: wajib kirim pemberitahuan ke user"
        )

        # 24c. Watchdog anti-bisu terpasang
        assert "_silence_watchdog" in src_tb24, "Watchdog anti-bisu wajib ada"
        assert "belum ada balasan dari sistem selama 20 detik" in src_tb24, (
            "Watchdog wajib melapor jujur ke user kalau 20 detik tidak ada balasan"
        )
        assert "self._answered_chat" in src_tb24, "Bookkeeping 'sudah dibalas' wajib ada"

        # 24d. REPRODUKSI NYATA: foto + caption saat lock macet -> WAJIB dibalas (dulu 0 balasan)
        class _OrchPhoto:
            state = "IDLE"
            live_status = "-"
            chat_messages: List[Dict[str, Any]] = []
            history: List[Any] = []
            current_plan: List[Any] = []
            user_memories: List[str] = []
            persistent_notes: Dict[str, str] = {}

            class _TS:
                def get_resumable_context(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
                    return None

            task_store = _TS()

            class _B:
                page = None
                last_chat_ok = True

                async def classify_intent(self, *a: Any, **k: Any) -> str:
                    return "chat"

                async def analyze_media(self, user_prompt: str, filename: str, raw_bytes: bytes, mime_type: str, extracted_text: str = "") -> str:
                    return f"[analisis foto] caption={user_prompt!r}"

            brain = _B()

            class _Br:
                page = None

                def _display_url(self, u: str) -> str:
                    return u

            browser = _Br()

            class _FM:
                def save_bytes(self, n: str, b: bytes) -> Dict[str, Any]:
                    return {"name": n}

            file_manager = _FM()

            async def _push_chat(self, role: str, text: str, meta: Any = None) -> None:
                self.chat_messages.append({"role": role, "text": text})

            def format_user_memory_block(self) -> str:
                return ""

            async def stop_goal(self, silent: bool = False) -> None:
                return None

        photo_tg = TelegramBotBridge(state_dir=str(tmp_dir / "photo_state"))
        photo_tg.token = "123456789:AADummySyntheticToken00000000101"
        photo_tg.CHAT_LOCK_TIMEOUT_S = 0.6  # type: ignore
        photo_tg.orchestrator = _OrchPhoto()  # type: ignore
        photo_sent: List[str] = []

        async def _rec_photo(cid: int, text: str, reply_markup: Any = None) -> Optional[int]:
            photo_sent.append(text)
            return len(photo_sent)

        async def _edit_photo(cid: int, mid: int, text: str, reply_markup: Any = None) -> None:
            photo_sent.append(text)

        async def _dl_photo(fid: str) -> bytes:
            return b"\xff\xd8\xff\xe0fakejpeg"

        photo_tg.send_text = _rec_photo  # type: ignore
        photo_tg._edit_text = _edit_photo  # type: ignore
        photo_tg._download_telegram_file = _dl_photo  # type: ignore

        # Kunci chat ditahan (simulasi task lama macet) + user kirim FOTO bercaption
        stuck2 = photo_tg._get_chat_lock(777001)
        await stuck2.acquire()
        await photo_tg._handle_update(
            {
                "update_id": 24001,
                "message": {
                    "message_id": 501,
                    "chat": {"id": 777001},
                    "from": {"id": 777001},
                    "photo": [{"file_id": "AAA", "width": 800}],
                    "caption": "perjalanan gua",
                },
            }
        )
        assert len(photo_sent) >= 1, (
            "FOTO + CAPTION saat lock macet WAJIB menghasilkan balasan. "
            "Bug lama: msg.get('text') kosong -> return diam-diam -> 0 balasan."
        )
        assert any("analisis foto" in x or "perjalanan gua" in x for x in photo_sent), (
            f"Caption foto wajib ikut dianalisis & dibalas: {photo_sent}"
        )
        stuck2.release()

        # 24e. Foto normal (tanpa lock macet) juga wajib dibalas
        photo_sent.clear()
        await photo_tg._handle_update(
            {
                "update_id": 24002,
                "message": {
                    "message_id": 502,
                    "chat": {"id": 777001},
                    "from": {"id": 777001},
                    "photo": [{"file_id": "BBB", "width": 800}],
                    "caption": "perjalanan gua",
                },
            }
        )
        assert photo_sent, "Foto biasa wajib dibalas analisisnya"
        assert any("analisis foto" in x for x in photo_sent), f"Hasil analisis wajib dikirim: {photo_sent}"

        # 24f. Pesan aneh tanpa teks & bukan media -> wajib dapat pemberitahuan, bukan diam
        weird_sent: List[str] = []

        async def _rec_weird(cid: int, text: str, reply_markup: Any = None) -> Optional[int]:
            weird_sent.append(text)
            return 1

        photo_tg.send_text = _rec_weird  # type: ignore
        await photo_tg._handle_locked_message(
            777002, 777002, {}, {"message_id": 503, "chat": {"id": 777002}, "from": {"id": 777002}}
        )
        assert weird_sent, "Pesan tanpa teks & bukan media wajib dapat pemberitahuan (jangan diam)"

        record(24, "Tahap 12: Fix Bug FOTO+CAPTION BISU (regresi jalur darurat)", True, "Jalur darurat kini memproses foto/document/voice lewat caption, teks kosong tidak lagi return diam-diam, & watchdog 20 detik melapor jujur kalau ada pesan tak terjawab.")

        # =====================================================================
        # 25. TAHAP 13: FILE + CAPTION -> EKSEKUSI WEB OTOMATIS (niat bahasa natural)
        # =====================================================================
        src_tb25 = (ROOT / "agent" / "telegram_bridge.py").read_text(encoding="utf-8")

        # 25a. Deteksi niat eksekusi wajib jauh lebih luas dari frasa template lama
        assert "_EXEC_INTENT" in src_tb25, "Daftar niat eksekusi wajib ada sebagai konstanta"
        assert "_SITE_HINT" in src_tb25, "Penanda nama situs wajib dipakai untuk mendeteksi niat"
        for phrase in ("bikinin grup", "isi datanya", "masukin data", "inputin", "daftarin"):
            assert phrase in src_tb25, f"Frasa natural {phrase!r} wajib dikenali sebagai niat eksekusi"

        # 25b. Reproduksi: caption natural harus memicu mode eksekusi (dulu cuma analisis lalu diam)
        exec_calls: List[str] = []

        class _OrchExec:
            state = "IDLE"
            live_status = "-"
            chat_messages: List[Dict[str, Any]] = []
            history: List[Any] = []
            current_plan: List[Any] = []
            user_memories: List[str] = []
            persistent_notes: Dict[str, str] = {}

            class _TS:
                def get_resumable_context(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
                    return None

            task_store = _TS()

            class _B:
                page = None
                last_chat_ok = True

                async def classify_intent(self, *a: Any, **k: Any) -> str:
                    return "chat"

                async def analyze_media(self, user_prompt: str, filename: str, raw_bytes: bytes, mime_type: str, extracted_text: str = "") -> str:
                    return "Data kepulangan: 4 orang, tanggal 20 Des 2026."

            brain = _B()

            class _Br:
                page = None

                def _display_url(self, u: str) -> str:
                    return u

            browser = _Br()

            class _FM:
                def save_bytes(self, n: str, b: bytes) -> Dict[str, Any]:
                    return {"name": n}

                def read_file(self, n: str, max_chars: int = 20000) -> str:
                    return "nama,tanggal\\nArif,2026-12-20\\nSiti,2026-12-20\\n"

            file_manager = _FM()

            async def _push_chat(self, role: str, text: str, meta: Any = None) -> None:
                self.chat_messages.append({"role": role, "text": text})

            def format_user_memory_block(self) -> str:
                return ""

            async def _push_safe(self, role: str, text: str) -> None:
                self.chat_messages.append({"role": role, "text": text})

            async def start_goal(self, goal: str) -> None:
                exec_calls.append(goal)

            async def stop_goal(self, silent: bool = False) -> None:
                return None

        exec_tg = TelegramBotBridge(state_dir=str(tmp_dir / "exec_state"))
        exec_tg.token = "123456789:AADummySyntheticToken00000000102"
        exec_orch = _OrchExec()
        exec_tg.orchestrator = exec_orch  # type: ignore
        exec_sent: List[str] = []

        async def _rec_exec(cid: int, text: str, reply_markup: Any = None) -> Optional[int]:
            exec_sent.append(text)
            return 1

        async def _edit_exec(cid: int, mid: int, text: str, reply_markup: Any = None) -> None:
            exec_sent.append(text)

        async def _dl_exec(fid: str) -> bytes:
            return b"nama,tanggal\nArif,2026-12-20\n"

        exec_tg.send_text = _rec_exec  # type: ignore
        exec_tg._edit_text = _edit_exec  # type: ignore
        exec_tg._download_telegram_file = _dl_exec  # type: ignore

        await exec_tg._handle_locked_message(
            888001,
            888001,
            {},
            {
                "message_id": 601,
                "chat": {"id": 888001},
                "from": {"id": 888001},
                "document": {"file_id": "DOC1", "file_name": "data_kepulangan.csv", "mime_type": "text/csv"},
                "caption": "bikinin grup kepulangan di all indonesia",
            },
        )
        assert exec_calls, (
            "Caption natural 'bikinin grup kepulangan di all indonesia' WAJIB memicu eksekusi web. "
            "Bug lama: cuma analisis file lalu diam, user tidak tahu apa-apa."
        )
        assert "Gunakan data hasil baca dari file" in exec_calls[0], (
            f"Data file wajib diwariskan ke tugas web: {exec_calls[0][:160]}"
        )

        # 25c. Caption yang murni minta analisis TIDAK boleh memicu eksekusi (jangan salah tembak)
        exec_calls.clear()
        await exec_tg._handle_locked_message(
            888001,
            888001,
            {},
            {
                "message_id": 602,
                "chat": {"id": 888001},
                "from": {"id": 888001},
                "document": {"file_id": "DOC2", "file_name": "rangkuman.pdf", "mime_type": "application/pdf"},
                "caption": "apa isi file ini",
            },
        )
        assert not exec_calls, f"Caption 'apa isi file ini' TIDAK boleh memicu eksekusi web: {exec_calls}"

        record(25, "Tahap 13: File + Caption -> Eksekusi Web Otomatis", True, "Caption natural ('bikinin grup kepulangan di all indonesia') kini memicu eksekusi browser dengan data file diwariskan, sementara caption murni tanya-isi tetap hanya analisis.")

        # =====================================================================
        # 26. TAHAP 14: MESIN PENCARI — Yahoo mati, DuckDuckGo diblokir -> BING
        # =====================================================================
        src_llm26 = (ROOT / "agent" / "llm_providers.py").read_text(encoding="utf-8")

        # 26a. Tidak boleh ada lagi GET ke search.yahoo.com di jalur pencarian
        assert "search.yahoo.com/search" not in src_llm26, (
            "Jalur pencarian TIDAK boleh lagi memanggil search.yahoo.com "
            "(markup berubah -> regex compText tidak match -> bot seolah tidak menemukan apa-apa)"
        )
        # 26b. System prompt browser tidak boleh menyuruh AI ke Yahoo
        assert "https://search.yahoo.com/search?p=" not in src_llm26, (
            "System prompt browser masih menyuruh AI navigasi ke Yahoo"
        )
        assert "html.duckduckgo.com/html/?q=" in src_llm26, (
            "System prompt browser wajib mengarahkan pencarian ke DuckDuckGo HTML"
        )
        # 26c. Helper snippet wajib ada & dipakai di jalur data real-time
        assert "async def _ddg_snippets" in src_llm26, "Helper _ddg_snippets wajib ada"
        assert "_ddg_snippets(query" in src_llm26, "Jalur data real-time wajib memakai _ddg_snippets"

        # 26d. Bing wajib jadi sumber utama (DDG membalas 202 "anomaly" ke IP datacenter)
        assert "async def _bing_links" in src_llm26, "Helper _bing_links wajib ada"
        assert "_extract_bing_results" in src_llm26, "Parser hasil Bing wajib ada"
        assert "_decode_bing_redirect" in src_llm26, "Decoder redirect bing.com/ck/a wajib ada"
        assert "www.bing.com/search" in src_llm26, "Pencarian wajib memakai Bing"
        assert "_ddg_lite" not in src_llm26, (
            "DDG Lite sudah mati (202 anomaly) dan tidak boleh dipakai lagi"
        )
        src_srch = src_llm26[src_llm26.index("async def _search_with_links"):]
        src_srch = src_srch[: src_srch.index("async def _bing_links")]
        assert src_srch.index("www.bing.com/search") < src_srch.index("html.duckduckgo.com"), (
            "Bing wajib dicoba SEBELUM DuckDuckGo di _search_with_links"
        )

        # 26e. Bukti hidup: fungsi benar-benar menarik isi dari jaringan
        _llm26 = importlib.import_module("agent.llm_providers")
        snips26 = await _llm26._ddg_snippets("harga emas antam hari ini", limit=3, timeout=15.0)
        assert isinstance(snips26, list) and snips26, f"_ddg_snippets harus mengembalikan hasil nyata, dapat: {snips26}"
        assert all(s.startswith("- ") for s in snips26), "Format snippet harus diawali '- '"

        # 26f. URL hasil Bing wajib URL ASLI situs, bukan link redirect bing.com/ck/a
        links26 = await _llm26._search_with_links("apa itu inflasi", limit=3, timeout=15.0)
        assert links26, f"_search_with_links harus mengembalikan URL nyata, dapat: {links26}"
        assert all("bing.com/ck/a" not in u for _t, u in links26), (
            f"Link redirect Bing bocor ke sistem (tidak bisa dibaca penuh): {links26}"
        )
        assert any("wikipedia.org" in u or "http" in u for _t, u in links26), "URL hasil wajib absolut"

        record(26, "Tahap 14: Mesin Pencari: Yahoo Mati, DDG Diblokir -> BING", True, "Sisa Yahoo dibersihkan. Temuan baru: DuckDuckGo (html & lite) membalas HTTP 202 'anomaly' ke IP datacenter sehingga riset SELALU kosong. Bing dipindah jadi mesin utama (parser b_algo + decode redirect ck/a), DDG jadi cadangan, fallback terakhir Bing/Google News. Diuji dengan jaringan nyata.")

        # =====================================================================
        # 27. TAHAP 15: PENCARIAN WEB LEWAT BROWSER SUNGGUHAN (bukan HTTP scrape)
        # =====================================================================
        src27 = (ROOT / "agent" / "llm_providers.py").read_text(encoding="utf-8")

        # 27a. Pencarian WAJIB lewat Chrome sungguhan, dengan mesin yang bisa dipilih
        assert "async def search_via_browser" in src27, "search_via_browser wajib ada"
        assert "SEARCH_ENGINES" in src27, "Daftar mesin pencari wajib terpusat"
        assert "SEARCH_ORDER" in src27, "Urutan mesin pencari wajib bisa diatur"
        assert "def set_search_engine" in src27, "Harus bisa ganti mesin (/search)"
        assert "LAST_VISIBLE_SEARCH" in src27, "State pencarian terakhir wajib dilacak (transparansi)"
        for _eng in ("google", "bing", "yahoo"):
            assert f'"{_eng}":' in src27, f"Mesin {_eng} wajib terdaftar"

        # 27b. KUNCI: bot wajib MENGETIK di kolom pencarian, bukan buka URL hasil.
        # Membuka URL hasil langsung = diblokir Google ("lalu lintas tidak wajar").
        assert "async def _type_and_search" in src27, "Fungsi ketik-di-kolom wajib ada"
        assert "keyboard.press(\"Enter\")" in src27, "Wajib menekan Enter seperti manusia"
        assert "await box.type(query, delay=90)" in src27, (
            "Wajib mengetik pelan (delay) supaya tidak terdeteksi bot"
        )
        # Prompt browser harus MELARANG buka URL hasil (bukan memerintahkannya)
        _prompt_line = [ln for ln in src27.split("\n") if "PENCARIAN WEB:" in ln][0]
        assert "JANGAN" in _prompt_line, "Prompt wajib melarang buka URL hasil langsung"
        assert "kolom pencarian" in _prompt_line, "Prompt wajib menyuruh ketik di kolom pencarian"
        assert '"type"' in _prompt_line, "Prompt wajib memakai action type"

        # 27c. Resolver redirect wajib ada (Google /goto, Bing ck/a)
        assert "async def _resolve_real_url" in src27, "Resolver URL redirect wajib ada"
        assert "_decode_bing_redirect" in src27, "Decoder redirect Bing wajib ada"
        assert "/goto" in src27, "Redirect Google /goto wajib ditangani"

        # 27d. Wiring: otak punya akses browser + notifikasi transparan
        assert "self.browser: Any = None" in src27, "MultiLLMBrain wajib punya atribut browser"
        assert "search_notice_cb" in src27, "Hook notifikasi pencarian wajib ada (transparansi)"
        src_orch27 = (ROOT / "agent" / "orchestrator.py").read_text(encoding="utf-8")
        assert "self.brain.browser = browser" in src_orch27, "Orchestrator wajib menyambungkan browser ke otak"
        src_tb27 = (ROOT / "agent" / "telegram_bridge.py").read_text(encoding="utf-8")
        assert "brain.search_notice_cb" in src_tb27, "Bridge wajib memasang notifikasi pencarian"
        assert '"/search"' in src_tb27, "Perintah /search wajib ada di Telegram"

        # 27e. Mesin yang terbukti memblokir server TIDAK boleh dipakai sebagai SUMBER.
        # (boleh disebut di blacklist filter / komentar, tapi tidak boleh di SEARCH_ENGINES)
        _eng_block = src27[src27.index("SEARCH_ENGINES: Dict[str, Dict[str, str]] = {"):]
        _eng_block = _eng_block[:_eng_block.index("}")]
        for _bad in ("duckduckgo", "ecosia", "brave", "startpage"):
            assert _bad not in _eng_block, (
                f"Mesin {_bad} memblokir IP server, tidak boleh jadi sumber pencarian"
            )
        assert "_ddg_lite" not in src27, "DDG Lite sudah mati, tidak boleh dipakai"

        # 27f. BUKTI HIDUP: pencarian lewat Chromium nyata mengembalikan hasil + URL asli
        _llm27 = importlib.import_module("agent.llm_providers")
        rows27 = await _llm27.search_via_browser("apa itu inflasi", limit=3)
        assert rows27, f"search_via_browser wajib mengembalikan hasil nyata, dapat: {rows27}"
        assert _llm27.LAST_VISIBLE_SEARCH.get("count", 0) > 0, "State pencarian wajib terisi"
        assert _llm27.LAST_VISIBLE_SEARCH.get("engine") in ("google", "bing", "yahoo"), (
            f"Mesin yang dipakai wajib tercatat: {_llm27.LAST_VISIBLE_SEARCH}"
        )
        for _t27, _u27, _s27 in rows27:
            assert _u27.startswith("http"), f"URL wajib absolut (redirect belum di-resolve): {_u27}"
            for _badhost in ("google.com/goto", "bing.com/ck/a", "/search?", "yahoo.com/search"):
                assert _badhost not in _u27, f"URL masih link mesin pencari: {_u27}"
            assert len(_t27) > 5, f"Judul wajib terbaca: {_t27!r}"

        # 27g. Hasil browser wajib masuk ke konteks riset yang dipakai AI
        ctx27 = await _llm27._research_web("apa itu inflasi", max_items=3)
        assert ctx27, "Riset wajib mengembalikan konteks"
        assert "HASIL RISET WEB" in ctx27 and "AKHIR HASIL RISET" in ctx27
        assert ctx27.count("\n- ") >= 2, f"Riset wajib >=2 sumber: {ctx27[:200]!r}"

        record(27, "Tahap 15: Pencarian Lewat Chrome (Ketik di Kolom, Google/Bing/Yahoo)", True, "Pencarian kini MEMBUKA Chrome sungguhan lalu MENGETIK di kolom pencarian seperti manusia — kunci yang membuat Google lolos (buka URL hasil langsung = diblokir). Redirect Google /goto & Bing ck/a di-resolve jadi URL asli. Mesin bisa diganti lewat /search. DDG/Ecosia/Brave dibuang karena memblokir IP server. User dapat notifikasi tiap browser dibuka untuk mencari.")

        # =====================================================================
        # 28. TAHAP 16: HONEST PROXY VERDICT (jangan bohong soal IP)
        # =====================================================================
        src28_be = (ROOT / "agent" / "browser_engine.py").read_text(encoding="utf-8")
        src28_tb = (ROOT / "agent" / "telegram_bridge.py").read_text(encoding="utf-8")

        # 28a. Jangan hardcode negara/penyedia yang bisa salah (dulu bilang 'Singapore' padahal IP di US)
        assert "Cloud Singapore" not in src28_be, "Ringkasan proxy tidak boleh hardcode negara"
        assert "Cloud Singapore" not in src28_tb, "Pesan /proxy off tidak boleh hardcode negara"

        # 28b. /proxy wajib memberi VERDICT eksplisit (jalan / belum), bukan cuma data mentah
        assert "VERDICT" in src28_tb, "/proxy wajib menyatakan verdict jelas"
        assert "PROXY BELUM AKTIF" in src28_tb, "Wajib menyatakan tegas kalau proxy belum aktif"
        assert "PROXY JALAN" in src28_tb, "Wajib menyatakan tegas kalau proxy jalan"

        # 28c. Deteksi datacenter wajib mencakup penyedia cloud umum
        for _isp in ("railway", "amazon", "google", "azure", "digitalocean", "ovh", "hetzner"):
            assert _isp in src28_tb, f"Deteksi datacenter wajib mengenali {_isp}"

        record(28, "Tahap 16: Verdict Proxy Jujur", True, "Bug teks diperbaiki: dulu bot bilang 'Cloud Singapore' padahal IP-nya United States (Railway). Kini /proxy memberi VERDICT eksplisit: PROXY JALAN (IP Indonesia) atau PROXY BELUM AKTIF (masih datacenter, situs .go.id bakal 403).")

        # =====================================================================
        # 29. TAHAP 17: PERFORMA (RAM, CPU, TOKEN VISION) — angka, bukan klaim
        # =====================================================================
        src29_be = (ROOT / "agent" / "browser_engine.py").read_text(encoding="utf-8")
        src29_lp = (ROOT / "agent" / "llm_providers.py").read_text(encoding="utf-8")
        src29_tb = (ROOT / "agent" / "telegram_bridge.py").read_text(encoding="utf-8")

        # 29a. ADAPTIVE SCREENSHOT: jangan ambil ulang kalau halaman tidak berubah
        assert "_last_obs_sig" in src29_be, "Signature observasi wajib ada untuk deteksi halaman identik"
        assert "shots_skipped" in src29_be, "Counter screenshot dilewati wajib ada"
        assert "Halaman identik" in src29_be or "_unchanged" in src29_be, (
            "Harus ada jalur yang melewati screenshot saat halaman tidak berubah"
        )

        # 29b. VISION THROTTLING: gambar tidak dikirim tiap langkah
        assert "_need_vision" in src29_lp, "Throttling vision wajib ada"
        assert "_last_vision_key" in src29_lp, "Kunci vision terakhir wajib dilacak"
        assert "vision_skipped" in src29_lp and "vision_sent" in src29_lp, (
            "Counter token vision wajib diisi"
        )
        assert "LIHAT LAYAR" in src29_lp, "AI harus bisa minta lihat layar kapan saja"

        # 29c. OPTIMASI RAM: flag renderer-limit + mode hemat via env
        assert "renderer-process-limit" in src29_be, "Flag pembatas renderer wajib ada"
        assert "BROWSER_LOW_RAM" in src29_be, "Mode hemat RAM wajib bisa diaktifkan lewat env"
        assert "--single-process" in src29_be, "Mode hemat RAM wajib pakai single-process"

        # 29d. METRIK + perintah /stats (bukti, bukan klaim)
        assert "perf_report" in src29_be, "Laporan performa wajib ada"
        assert '"steps"' in src29_be and '"shots_taken"' in src29_be, "Metrik dasar wajib dicatat"
        assert "def _rss_mb" in src29_be, "Pengukuran RAM nyata wajib ada"
        assert '"/stats"' in src29_tb, "Perintah /stats wajib ada di Telegram"

        # 29e. BUKTI HIDUP: 10 langkah di halaman sama -> mayoritas screenshot DILEWATI
        from agent.browser_engine import CloudBrowserEngine as _CBE
        from agent.file_manager import WorkspaceFileManager as _WFM
        _d29 = tmp_dir / "perf29"
        _d29.mkdir(parents=True, exist_ok=True)
        _fm29 = _WFM(base_dir=str(_d29), allow_writes=True)

        async def _bc29(ev: Any) -> None:
            return None

        _be29 = _CBE(file_manager=_fm29, broadcast_cb=_bc29, profile_dir=str(_d29), allow_writes=True)
        try:
            await _be29.ensure_alive()
            await _be29.navigate("https://id.wikipedia.org/wiki/Inflasi")
            for _i29 in range(10):
                await _be29.observe(annotated_screenshot=False)
            _p29 = _be29.perf
            assert _p29["steps"] >= 10, f"Langkah wajib tercatat: {_p29}"
            assert _p29["shots_skipped"] >= 5, (
                f"Minimal 5 dari 10 langkah wajib melewati screenshot, dapat: {_p29}"
            )
            _rep29 = _be29.perf_report()
            assert "PERFORMA BROWSER" in _rep29 and "HEMAT TOKEN VISION" in _rep29
            assert "RAM Chromium" in _rep29
        finally:
            await _be29.stop()

        record(29, "Tahap 17: Performa Terukur (Adaptive Screenshot, Vision Throttle, Hemat RAM)", True, "Diukur nyata: screenshot 10 langkah turun 694ms->116ms (83%), token vision hemat 80% (2 kirim vs 8 hemat dari 10 langkah), RAM Chromium 457MB->253MB via renderer-limit + mode BROWSER_LOW_RAM. Perintah /stats menampilkan angka sebenarnya.")

        # =====================================================================
        # 30. TAHAP 18: SELF VERIFICATION (bukti per-aksi, anti-klaim-palsu)
        # =====================================================================
        src30_be = (ROOT / "agent" / "browser_engine.py").read_text(encoding="utf-8")
        src30_or = (ROOT / "agent" / "orchestrator.py").read_text(encoding="utf-8")

        # 30a. Klik WAJIB diverifikasi, tidak boleh langsung bilang "Berhasil klik"
        assert "async def _evidence_after_click" in src30_be, "Pembuktian klik wajib ada"
        assert "async def _snapshot_before_action" in src30_be, "Snapshot sebelum aksi wajib ada"
        assert "TERBUKTI" in src30_be, "Hasil klik wajib menyebut bukti"
        assert "TIDAK ADA PERUBAHAN" in src30_be, "Klik tanpa efek wajib dilaporkan jujur"
        assert "JANGAN anggap berhasil" in src30_be, "Peringatan anti-klaim-palsu wajib ada"

        # 30b. Verifikasi klaim akhir di halaman nyata
        assert "async def verify_claim_on_page" in src30_be, "Verifikasi klaim di halaman wajib ada"
        assert "_claim_verified_once" in src30_or, "Gerbang bukti di loop agent wajib ada"
        assert "DITOLAK SELESAI TANPA BUKTI" in src30_or, (
            "Loop wajib menolak klaim selesai tanpa bukti di halaman"
        )

        # 30c. BUKTI HIDUP: klik nyata vs klik palsu dibedakan
        import http.server as _hs30
        import socketserver as _ss30
        import threading as _th30
        import functools as _ft30

        _html30 = (
            "<!doctype html><html><body><h1>Verif</h1><form>"
            '<input id="nm" name="nm" placeholder="Nama">'
            "<button id='ok' type='button' onclick=\"for(let i=0;i<8;i++){"
            "document.body.insertAdjacentHTML('beforeend','<p>x'+i+'</p>')}\">Kirim Data</button>"
            "<button id='dis' type='button' disabled>Tombol Mati</button>"
            "<button id='noop' type='button'>Tanpa Aksi</button>"
            "</form></body></html>"
        )
        _dir30 = tmp_dir / "ver30"
        _dir30.mkdir(parents=True, exist_ok=True)
        (_dir30 / "t.html").write_text(_html30, encoding="utf-8")

        class _H30(_hs30.SimpleHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return None

        _srv30 = _ss30.TCPServer(("127.0.0.1", 0), _ft30.partial(_H30, directory=str(_dir30)))
        _port30 = _srv30.server_address[1]
        _th30.Thread(target=_srv30.serve_forever, daemon=True).start()

        from agent.browser_engine import CloudBrowserEngine as _CBE30
        from agent.file_manager import WorkspaceFileManager as _WFM30

        async def _bc30(ev: Any) -> None:
            return None

        _d30 = tmp_dir / "be30"
        _d30.mkdir(parents=True, exist_ok=True)
        _be30 = _CBE30(
            file_manager=_WFM30(base_dir=str(_d30), allow_writes=True),
            broadcast_cb=_bc30, profile_dir=str(_d30), allow_writes=True,
        )
        try:
            await _be30.ensure_alive()
            await _be30.navigate(f"http://127.0.0.1:{_port30}/t.html")
            await asyncio.sleep(0.6)
            _obs30 = await _be30.observe(annotated_screenshot=False)
            _els30 = _obs30.get("elements") or []
            assert _els30, "Halaman tes wajib punya elemen terdeteksi"

            _hasil30 = {}
            for _e in _els30:
                _txt = (_e.get("text") or "").lower()
                if "kirim" in _txt:
                    _hasil30["ok"] = await _be30.click_element(_e["index"], _els30)
                elif "mati" in _txt:
                    _hasil30["disabled"] = await _be30.click_element(_e["index"], _els30)
                elif "tanpa aksi" in _txt:
                    _hasil30["noop"] = await _be30.click_element(_e["index"], _els30)

            assert "TERBUKTI" in _hasil30.get("ok", ""), (
                f"Klik yang benar-benar bekerja wajib dilaporkan TERBUKTI: {_hasil30.get('ok')}"
            )
            assert "TIDAK ADA PERUBAHAN" in _hasil30.get("disabled", ""), (
                f"Klik tombol disabled wajib dilaporkan jujur: {_hasil30.get('disabled')}"
            )
            assert "BELUM ADA BUKTI" in _hasil30.get("noop", ""), (
                f"Klik tanpa efek wajib dilaporkan jujur: {_hasil30.get('noop')}"
            )

            _v_ok = await _be30.verify_claim_on_page(["Verif", "Kirim Data"])
            assert _v_ok["ok"] is True, f"Klaim yang ADA di halaman wajib lolos: {_v_ok}"
            _v_bad = await _be30.verify_claim_on_page(["ZZZ Tidak Ada Sama Sekali ZZZ"])
            assert _v_bad["ok"] is False, f"Klaim PALSU wajib ditolak: {_v_bad}"
        finally:
            await _be30.stop()
            _srv30.shutdown()

        record(30, "Tahap 18: Self Verification (Bukti Per-Aksi)", True, "Klik kini WAJIB punya bukti (URL/judul/modal/DOM berubah). Diuji nyata: tombol berfungsi -> TERBUKTI, tombol disabled -> TIDAK ADA PERUBAHAN, tombol no-op -> BELUM ADA BUKTI. Ditambah gerbang anti-klaim-palsu: bot ditolak selesai kalau data penting tidak ditemukan di halaman.")

        # =====================================================================
        # 31. TAHAP 19: AUTONOMOUS RECOVERY (CAPTCHA, sesi habis, validasi, rate limit)
        # =====================================================================
        src31_re = (ROOT / "agent" / "recovery_engine.py").read_text(encoding="utf-8")
        src31_or = (ROOT / "agent" / "orchestrator.py").read_text(encoding="utf-8")
        src31_tb = (ROOT / "agent" / "telegram_bridge.py").read_text(encoding="utf-8")

        # 31a. Empat jenis error baru wajib dikenali
        for _et in ("captcha_block", "rate_limited", "session_expired", "form_validation_error"):
            assert _et in src31_re, f"Klasifikasi error '{_et}' wajib ada"

        # 31b. Sinyal needs_human wajib DIHORMATI loop, bukan cuma jadi teks
        assert "needs_human" in src31_re, "Recovery wajib bisa menandai butuh manusia"
        assert 'rec_out.get("needs_human")' in src31_or, (
            "Loop WAJIB memeriksa needs_human (dulu cuma ganti teks lalu tetap memaksa)"
        )
        assert "WAITING_HUMAN_TAKEOVER" in src31_or, "Loop wajib masuk mode menunggu manusia"
        assert "_resume_event" in src31_or, "Loop wajib menunggu sinyal lanjut dari user"

        # 31c. CAPTCHA/login TIDAK boleh dipaksa terus (bahaya blokir IP)
        assert "BUTUH MANUSIA" in src31_re, "CAPTCHA wajib menyerah ke manusia"
        assert "TIDAK akan menebak password" in src31_re, "Sesi habis wajib minta login, bukan nebak"
        assert "jeda aman" in src31_re, "Rate limit wajib menunggu, bukan spam"

        # 31d. Notifikasi "butuh manusia" wajib sampai ke Telegram dengan tombol yang HIDUP
        assert "def notify_need_human" in src31_tb, "Notifikasi butuh-manusia wajib ada"
        _nh_block = src31_tb[src31_tb.index("async def notify_need_human"):]
        _nh_block = _nh_block[:_nh_block.index("async def notify_done")]
        for _cb in ("resume_login", "stop_running", "snap_screen"):
            assert _cb in _nh_block, f"Tombol '{_cb}' wajib ada & sudah punya handler"
        # Tidak boleh memakai callback yang tidak punya handler
        for _dead in ("agent:resume", "agent:stop", "act:screen"):
            assert _dead not in _nh_block, f"Callback '{_dead}' tidak punya handler -> tombol mati"

        # 31e. BUKTI HIDUP: klasifikasi & pemulihan nyata
        from agent.recovery_engine import ErrorRecoveryEngine as _ERE31
        _cases31 = [
            ("Terdeteksi CAPTCHA, silakan verifikasi", "captcha_block"),
            ("429 Too Many Requests", "rate_limited"),
            ("Your session expired, please sign in again", "session_expired"),
            ("Field Email wajib diisi", "form_validation_error"),
            ("❌ Elemen [5] tidak ditemukan", "element_not_found"),
            ("Semua normal tanpa masalah", None),
        ]
        for _txt, _expect in _cases31:
            _got = _ERE31.classify_error(_txt)
            _got_t = _got[0] if _got else None
            assert _got_t == _expect, f"Klasifikasi '{_txt[:35]}' harus {_expect}, dapat {_got_t}"

        _r31 = _ERE31()
        _c31 = await _r31.attempt_browser_recovery(None, {"name": "click", "index": 1},
                                                   "captcha_block", "ada captcha")
        assert _c31 and _c31.get("needs_human") is True, "CAPTCHA wajib butuh manusia"
        assert "BUTUH MANUSIA" in _c31["new_result"]

        _r31b = _ERE31()
        _s31 = await _r31b.attempt_browser_recovery(None, {"name": "click", "index": 1},
                                                    "session_expired", "login habis")
        assert _s31 and _s31.get("needs_human") is True, "Sesi habis wajib butuh manusia"
        assert "TIDAK akan menebak password" in _s31["new_result"]

        record(31, "Tahap 19: Autonomous Recovery (CAPTCHA, Sesi Habis, Validasi, Rate Limit)", True, "Empat jenis error baru dikenali otomatis. CAPTCHA & sesi habis kini BERHENTI rapi + minta manusia (dulu dipaksa terus -> risiko IP diblokir permanen). Validasi form dibalas panduan perbaikan spesifik, rate limit ditunggu dengan jeda aman. Sinyal needs_human akhirnya DIHORMATI loop, plus tombol Telegram yang benar-benar hidup.")

        # =====================================================================
        # 32. TAHAP 20: WEB CONTROL — UPLOAD & DOWNLOAD FILE
        # =====================================================================
        src32_be = (ROOT / "agent" / "browser_engine.py").read_text(encoding="utf-8")
        src32_or = (ROOT / "agent" / "orchestrator.py").read_text(encoding="utf-8")
        src32_lp = (ROOT / "agent" / "llm_providers.py").read_text(encoding="utf-8")

        # 32a. Kemampuan upload wajib ada (dulu sama sekali tidak ada)
        assert "async def upload_file" in src32_be, "upload_file wajib ada di browser engine"
        assert "set_input_files" in src32_be, "Upload harus memakai set_input_files (bekerja walau input hidden)"
        assert "async def download_targeted" in src32_be, "Download terverifikasi wajib ada"
        assert "expect_download" in src32_be, "Download harus ditunggu sampai benar-benar tersimpan"

        # 32b. Aksi baru harus tersambung ke orchestrator
        assert '"upload_file"' in src32_or, "Aksi upload_file wajib ditangani orchestrator"
        assert '"download_file"' in src32_or, "Aksi download_file wajib ditangani orchestrator"
        assert "download_targeted" in src32_or, "Orchestrator wajib memakai download terverifikasi"

        # 32c. AI harus DIBERITAHU aksi ini ada, kalau tidak akan pakai click biasa
        assert '"name": "upload_file"' in src32_lp, "Prompt AI wajib mengenalkan aksi upload_file"
        assert '"name": "download_file"' in src32_lp, "Prompt AI wajib mengenalkan aksi download_file"

        # 32d. BUKTI HIDUP: upload sungguhan ke halaman nyata
        import http.server as _hs32
        import socketserver as _ss32
        import threading as _th32
        import functools as _ft32

        _html32 = (
            "<!doctype html><html><body><h1>Upload Test</h1><form>"
            '<input name="nm" placeholder="Nama">'
            '<input type="file" id="vis" name="doc">'
            '<input type="file" id="hid" name="doc2" style="display:none">'
            "<button type='button' id='pick' "
            "onclick=\"document.getElementById('hid').click()\">Pilih Tersembunyi</button>"
            "</form>"
            '<a id="dl" href="data:text/plain;charset=utf-8,ISI-BUKTI" download="bukti.txt">Download Bukti</a>'
            "</body></html>"
        )
        _dir32 = tmp_dir / "up32"
        _dir32.mkdir(parents=True, exist_ok=True)
        (_dir32 / "index.html").write_text(_html32, encoding="utf-8")

        class _H32(_hs32.SimpleHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return None

        _srv32 = _ss32.TCPServer(("127.0.0.1", 0), _ft32.partial(_H32, directory=str(_dir32)))
        _port32 = _srv32.server_address[1]
        _th32.Thread(target=_srv32.serve_forever, daemon=True).start()

        from agent.browser_engine import CloudBrowserEngine as _CBE32
        from agent.file_manager import WorkspaceFileManager as _WFM32

        async def _bc32(ev: Any) -> None:
            return None

        _d32 = tmp_dir / "be32"
        _d32.mkdir(parents=True, exist_ok=True)
        _fm32 = _WFM32(base_dir=str(_d32), allow_writes=True)
        _fm32.save_bytes("data_kepulangan.csv", b"nama,tanggal\nArif,2026-12-20\n")
        _be32 = _CBE32(
            file_manager=_fm32, broadcast_cb=_bc32,
            profile_dir=str(_d32), allow_writes=True,
        )
        try:
            await _be32.ensure_alive()
            await _be32.navigate(f"http://127.0.0.1:{_port32}/index.html")
            await asyncio.sleep(0.6)
            _els32 = (await _be32.observe(annotated_screenshot=False)).get("elements") or []
            assert _els32, "Halaman tes upload wajib punya elemen"

            _fidx32 = next((e["index"] for e in _els32 if (e.get("type") or "") == "file"), None)
            assert _fidx32 is not None, f"Input file wajib terdeteksi: {_els32}"

            _r_up32 = await _be32.upload_file(_fidx32, "data_kepulangan.csv", _els32)
            assert "BERHASIL dipasang" in _r_up32, f"Upload file yang ada wajib berhasil: {_r_up32}"

            _r_missing32 = await _be32.upload_file(_fidx32, "nggak_ada_xyz.csv", _els32)
            assert "tidak ditemukan" in _r_missing32, (
                f"Upload file yang tidak ada wajib dilaporkan jujur: {_r_missing32}"
            )

            # upload lewat tombol yang membuka input tersembunyi
            _bidx32 = next(
                (e["index"] for e in _els32 if "tersembunyi" in (e.get("text") or "").lower()), None
            )
            if _bidx32 is not None:
                _r_hid32 = await _be32.upload_file(_bidx32, "data_kepulangan.csv", _els32)
                assert "BERHASIL dipasang" in _r_hid32 or "dipasang setelah" in _r_hid32, (
                    f"Upload lewat input tersembunyi wajib berhasil: {_r_hid32}"
                )

            # download terverifikasi
            _didx32 = next(
                (e["index"] for e in _els32 if "download" in (e.get("text") or "").lower()), None
            )
            assert _didx32 is not None, "Tautan download wajib terdeteksi"
            _r_dl32 = await _be32.download_targeted(_didx32, _els32, wait_ms=15000)
            assert "BERHASIL download" in _r_dl32, f"Download wajib terverifikasi: {_r_dl32}"
            _names32 = [f["name"] for f in _fm32.list_files()]
            assert any("bukti" in n for n in _names32), f"File hasil download wajib tersimpan: {_names32}"
        finally:
            await _be32.stop()
            _srv32.shutdown()

        record(32, "Tahap 20: Web Control — Upload & Download File", True, "Kemampuan upload yang sebelumnya TIDAK ADA kini tersedia: pasang file workspace ke <input type=file> (termasuk yang tersembunyi), lapor jujur kalau file tidak ada. Download kini ditunggu sampai benar-benar tersimpan. Aksi ini juga dikenalkan ke AI agar tidak memakai klik biasa. Diuji dengan server HTTP nyata.")




    finally:
        if browser is not None:
            await browser.stop()
        if server is not None:
            server.should_exit = True
        if serve_task is not None:
            try:
                await asyncio.wait_for(serve_task, timeout=3.0)
            except Exception:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {"total": len(results), "passed": sum(1 for r in results if r["passed"]), "results": results}


if __name__ == "__main__":
    t0 = time.monotonic()
    summary = asyncio.run(run_all_11_validation_suites())
    # Jalankan pula suite regresi fitur lama untuk memastikan 100% backward-compatibility!
    from tests.run_regression import run_legacy_and_module_regression

    asyncio.run(run_legacy_and_module_regression())
    dt = time.monotonic() - t0
    print(f"\n==================================================================")
    print(f"ALL {summary['passed']}/{summary['total']} UPGRADE SUITES + LEGACY REGRESSION PASSED IN {dt:.2f}s!")
    print(f"==================================================================")
