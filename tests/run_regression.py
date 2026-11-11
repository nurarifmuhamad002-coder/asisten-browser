import asyncio
import os
import py_compile
import shutil
import tempfile
from pathlib import Path


async def run_legacy_and_module_regression() -> None:
    root = Path(__file__).resolve().parent.parent
    import sys
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    py_files = [root / "main.py", *sorted((root / "agent").glob("*.py"))]
    for pf in py_files:
        py_compile.compile(str(pf), doraise=True)

    from agent.browser_engine import CloudBrowserEngine
    from agent.file_manager import WorkspaceFileManager
    from agent.guardrails import inspect_element_sensitivity, inspect_non_browser_sensitivity, is_credential_element
    from agent.llm_providers import LLMConfig
    from agent.orchestrator import AgentOrchestrator
    from agent.recovery_engine import ErrorRecoveryEngine
    from agent.security import inspect_script_safety, redact_secrets, redact_structure
    from agent.task_store import TaskStateStore, compute_side_effect_fingerprint
    from agent.telegram_bridge import TelegramBotBridge

    tmp_root = Path(tempfile.mkdtemp(prefix="reg_test_"))
    try:
        # 1. Security redaction & script guardrails (using synthetic dummy values only)
        fake_tg = "123456789:AAFakeSyntheticTokenForUnitTestOnly12345"
        fake_gem = "AIzaSyFakeUnitTestingKey0000000000000001"
        raw_sample = f"https://api.telegram.org/bot{fake_tg}/getMe?key={fake_gem} password: rahasiaUser123"
        red = redact_secrets(raw_sample)
        assert fake_tg not in red and fake_gem not in red and "rahasiaUser123" not in red, f"Redaction failed: {red}"

        nested_red = redact_structure({"password": "mySecretPassword", "note": f"token={fake_tg}"})
        assert nested_red["password"] == "[REDACTED]" and fake_tg not in nested_red["note"]

        assert inspect_script_safety("import os\nos.remove('penting.xlsx')", "python")["decision"] == "approve"
        assert inspect_script_safety("import os\nprint(os.environ)", "python")["decision"] == "block"
        assert inspect_script_safety("print(2 + 2)", "python")["decision"] == "safe"
        assert inspect_non_browser_sensitivity({"name": "delete_file", "filename": "a.txt"})["decision"] == "approve"
        assert inspect_element_sensitivity({"tag": "button", "type": "submit", "text": "Konfirmasi Pembayaran"}) is not None
        assert is_credential_element({"tag": "input", "type": "password", "name": "pwd"}) is True

        # 2. Persistent Owner Lock + Webhook Secret across restarts
        sec_dir = tmp_root / "sec_state"
        tg1 = TelegramBotBridge(state_dir=str(sec_dir))
        tg1._lock_owner(777888999, {"username": "tester", "first_name": "Tester"})
        saved_pin = tg1.web_access_key
        saved_wh = tg1.webhook_secret
        tg1._webhook_secret_registered = True
        assert tg1.verify_webhook_request(saved_wh) is True
        assert tg1.verify_webhook_request("wrong_secret") is False

        tg2 = TelegramBotBridge(state_dir=str(sec_dir))
        assert tg2.is_locked and tg2.owner_id == 777888999, "Owner lock must survive restart!"
        assert tg2.web_access_key == saved_pin

        # 3. Legacy WorkspaceFileManager: DOCX, Multi-Sheet XLSX, PDF, Nested Folders, Tree, & ZIP
        ws_dir = tmp_root / "ws"
        fm = WorkspaceFileManager(str(ws_dir))
        fm.make_dir("DEMO_PROJ/KOSONG")
        fm.write_file("DEMO_PROJ/DOK/info.txt", "Isi catatan uji")
        tree_s = fm.get_directory_tree("DEMO_PROJ")
        assert "KOSONG/" in tree_s and "info.txt" in tree_s
        z_res = fm.zip_directory("DEMO_PROJ")
        assert (ws_dir / z_res["name"]).exists() and z_res["size"] > 100

        xlsx_res = fm.write_file(
            "rekap_uji.xlsx",
            "# Sheet 1: Data\n| Nama | Harga |\n|---|---|\n| Buku A | 25.5 |\n\n# Sheet 2: Ringkasan\n| Metrik | Nilai |\n|---|---|\n| Rata-rata | 25.5 |",
        )
        import openpyxl
        wb = openpyxl.load_workbook(str(fm.get_file_path(xlsx_res["name"])))
        assert wb.sheetnames == ["Data", "Ringkasan"], f"Unexpected sheets: {wb.sheetnames}"
        wb.close()

        docx_res = fm.export_formatted("surat_uji", "# Judul Surat\nIsi paragraf.", "docx")
        pdf_res = fm.export_formatted("surat_uji", "# Judul Surat\nIsi paragraf.", "pdf")
        assert docx_res["size"] > 500 and pdf_res["size"] > 500

        # 4. Legacy Proxy Parser & Memory (/ingat) & Auto-Router
        px = CloudBrowserEngine.parse_proxy_input("proxy.waroengproxy.com:8000:user01:pass01")
        assert px and "-cc-ID-" in px["username"]

        async def _noop(ev):
            pass

        be = CloudBrowserEngine(fm, _noop, profile_dir=str(tmp_root / "prof"))
        orch = AgentOrchestrator(be, fm, LLMConfig(), _noop, tg2)
        tg2.attach_orchestrator(orch)
        added = orch.add_user_memories("Nama: Budi\nKota: Jakarta")
        assert len(added) == 2 and "Budi" in orch.format_user_memory_block()

        r_folder, _ = await tg2._detect_auto_route(777888999, "Buat sistem folder PROJECT_X/ ├── DATA/", "buat sistem folder project_x/ ├── data/")
        r_web, _ = await tg2._detect_auto_route(777888999, "Buka https://example.com lalu rangkum isinya", "buka https://example.com lalu rangkum isinya")
        assert r_folder == "agent" and r_web == "agent"

        # 5. TaskStateStore: Concurrent Writes, Secret Redaction, Idempotency, & Process Restart Recovery
        ts_dir = tmp_root / "task_store"
        store1 = TaskStateStore(str(ts_dir))
        t_rec = store1.create_task(
            f"Isi form login dengan password: secretPass999 dan token {fake_tg}",
            initial_plan=[{"step": "Buka web", "status": "in_progress"}, {"step": "Submit", "status": "pending"}],
        )
        tid = t_rec["task_id"]
        assert "secretPass999" not in store1.main_file.read_text(encoding="utf-8")
        assert fake_tg not in store1.main_file.read_text(encoding="utf-8")

        # Concurrent writes from 8 parallel threads/coroutines
        fp_sub = compute_side_effect_fingerprint({"name": "click", "index": 5, "_is_side_effect": True, "_target_sig": "#submit"}, "https://example.com/form")
        await asyncio.gather(
            *[
                asyncio.to_thread(
                    store1.record_step_outcome,
                    tid,
                    step_i,
                    "type",
                    f"Langkah {step_i} sukses",
                    True,
                    "https://example.com/form",
                    fp_sub if step_i == 1 else None,
                )
                for step_i in range(1, 9)
            ]
        )
        assert store1.check_idempotency(tid, fp_sub) is not None
        store1.mark_step_running(tid, 9, "click", "Sedang klik tombol...")

        # Simulate sudden server crash/restart while status == RUNNING!
        store_after_restart = TaskStateStore(str(ts_dir))
        recovered = store_after_restart.get_task(tid)
        assert recovered and recovered["status"] == "TERHENTI", f"Expected TERHENTI after crash, got {recovered}"
        assert len(recovered["completed_steps"]) == 8
        assert len(recovered["recovery_log"]) >= 1
        res_ctx = store_after_restart.get_resumable_context()
        assert res_ctx and res_ctx["last_verified_url"] == "https://example.com/form"

        # 6. ErrorRecoveryEngine (MODUL 3) & DOM/Browser Engine (MODUL 2)
        rec = ErrorRecoveryEngine()
        assert rec.classify_error("❌ Elemen [99] tidak ditemukan berdasarkan DOM selector")[0] == "element_not_found"
        assert rec.classify_error("[UNREACHABLE_SITE] Gagal membuka https://foo.invalid")[0] == "page_load_failed"
        assert rec.classify_error("❌ SUBMIT GAGAL (Tabel hasil belum muncul)!")[0] == "website_error_message"

        fake_dom = {"url": "https://demo.local/form", "dom_signature": "sig_v1", "page_errors": ["Kolom wajib belum diisi"]}
        click_act = {"name": "click", "index": 3}
        assert rec.check_blind_repeat_before_action(click_act, fake_dom) is None
        rec.record_action_delta(click_act, fake_dom)
        # Klik ke-2 yang identik ketika DOM sama sekali tidak berubah WAJIB diblokir!
        blocked_msg = rec.check_blind_repeat_before_action(click_act, fake_dom)
        assert blocked_msg and "RECOVERY CIRCUIT BREAKER" in blocked_msg

        # Uji langsung Playwright Browser Automation + DOM page_errors + Screenshot Proof pada halaman lokal aman
        try:
            await be.ensure_alive()
            assert be.page is not None
            await be.page.set_content(
                """<!DOCTYPE html><html><head><title>Safe Demo Test</title></head><body>
                <h1>Portal Uji Aman</h1>
                <div role="alert" class="alert-danger">Nomor referensi belum valid</div>
                <form>
                  <label for="u_name">Nama Lengkap</label><input id="u_name" name="u_name" type="text" />
                  <label for="u_pin">Kode OTP</label><input id="u_pin" name="otp_code" type="text" value="889900" />
                  <select id="u_role"><option value="staf">Staf</option><option value="manajer">Manajer</option></select>
                  <button id="btn_ok" type="button" onclick="document.getElementById('out').innerText='OK:'+document.getElementById('u_name').value">Proses</button>
                </form>
                <p id="out"></p>
                </body></html>"""
            )
            dom_live = await be.observe(annotated_screenshot=False)
            assert any("Nomor referensi belum valid" in pe for pe in dom_live.get("page_errors", [])), f"page_errors missed: {dom_live.get('page_errors')}"
            els_live = dom_live["elements"]
            idx_name = next(e["index"] for e in els_live if e["el_id"] == "u_name")
            idx_role = next(e["index"] for e in els_live if e["el_id"] == "u_role")
            idx_btn = next(e["index"] for e in els_live if e["el_id"] == "btn_ok")

            t_res = await be.type_into_element(idx_name, "Andi Wijaya", els_live)
            assert "✅" in t_res
            s_res = await be.select_dropdown(idx_role, "Manajer", els_live)
            assert "✅" in s_res
            c_res = await be.click_element(idx_btn, els_live)
            assert "Berhasil klik" in c_res
            out_txt = await be.page.locator("#out").inner_text()
            assert out_txt == "OK:Andi Wijaya"

            # Verifikasi CSS masking input OTP aktif (-webkit-text-security == 'disc')
            sec_style = await be.page.locator("#u_pin").evaluate("el => getComputedStyle(el).webkitTextSecurity")
            assert sec_style == "disc", f"OTP input must be masked in screenshots, got: {sec_style}"
        finally:
            await be.stop()

        print("REGRESSION PASS (Modules 6 + 4 + 2 + 3 + Legacy): All checks 100% OK!")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(run_legacy_and_module_regression())
