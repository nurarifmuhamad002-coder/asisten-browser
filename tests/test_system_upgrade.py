import asyncio
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
            assert "API Key" in msg_auth and "sibuk" not in msg_auth.lower()

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
