import asyncio
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from agent.browser_engine import CloudBrowserEngine
from agent.file_manager import WorkspaceFileManager
from agent.guardrails import inspect_element_sensitivity, is_credential_element, is_practice_sandbox_url
from agent.llm_providers import LLMConfig, MultiLLMBrain
from agent.telegram_bridge import TelegramBotBridge


class AgentOrchestrator:
    """
    Otak orkestrasi multi-tahap dengan antarmuka gaya Chat WA/Telegram:
    Browser berjalan tenang di belakang layar, dan HANYA memunculkan layar
    secara otomatis ketika butuh Login user atau Persetujuan Tindakan Sensitif.
    """

    MAX_STEPS = 26

    def __init__(
        self,
        browser: CloudBrowserEngine,
        file_manager: WorkspaceFileManager,
        llm_config: LLMConfig,
        broadcast_cb: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]],
        telegram: Optional[TelegramBotBridge] = None,
    ):
        self.browser = browser
        self.file_manager = file_manager
        self.llm_config = llm_config
        self.brain = MultiLLMBrain(llm_config)
        self.broadcast = broadcast_cb
        self.telegram = telegram

        self.state: str = "IDLE"  # IDLE | RUNNING | WAITING_HUMAN_TAKEOVER | WAITING_APPROVAL
        self.live_status: str = "Online · Siap disuruh apa aja"
        self.manual_takeover: bool = False
        self.current_goal: str = ""
        self.current_plan: List[Dict[str, str]] = []
        self.history: List[Dict[str, Any]] = []
        self.memory_notes: Dict[str, str] = {}
        self.persistent_notes: Dict[str, str] = {}
        self._form_touched: bool = False
        self._expected_fields: Dict[str, str] = {}
        self._field_warnings: Dict[str, str] = {}
        self._last_submit_verification: Optional[Dict[str, Any]] = None
        self._verify_retry_used: bool = False
        self._pre_submit_blocked_once: bool = False
        self.chat_messages: List[Dict[str, Any]] = []

        self.pending_approval: Optional[Dict[str, Any]] = None
        self.takeover_reason: str = ""

        self._resume_event = asyncio.Event()
        self._approval_decision: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._stop_requested: bool = False

    def snapshot_state(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "live_status": self.live_status,
            "manual_takeover": self.manual_takeover or (self.state == "WAITING_HUMAN_TAKEOVER"),
            "goal": self.current_goal,
            "plan": self.current_plan,
            "chat_messages": self.chat_messages[-60:],
            "pending_approval": self.pending_approval,
            "takeover_reason": self.takeover_reason,
            "files": self.file_manager.list_files(),
            "llm": self.llm_config.public_dict(),
            "telegram": self.telegram.public_dict() if self.telegram else {},
            "som_badges": self.browser.show_som_badges,
            "url": self.browser._display_url(self.browser.page.url) if self.browser.page else "",
        }

    async def _push_chat(self, role: str, text: str, meta: Optional[Dict[str, Any]] = None) -> None:
        msg = {
            "id": f"m_{len(self.chat_messages) + 1}_{int(datetime.now().timestamp() * 1000)}",
            "role": role,  # user | agent | step | file | takeover | approval | system
            "text": text,
            "meta": meta or {},
            "time": datetime.now().strftime("%H:%M"),
        }
        self.chat_messages.append(msg)
        await self.broadcast({"type": "chat_message", "message": msg})

    async def _sync_state(self) -> None:
        await self.broadcast(
            {
                "type": "agent_state",
                "state": self.state,
                "live_status": self.live_status,
                "manual_takeover": self.manual_takeover or (self.state == "WAITING_HUMAN_TAKEOVER"),
                "goal": self.current_goal,
                "plan": self.current_plan,
                "pending_approval": self.pending_approval,
                "takeover_reason": self.takeover_reason,
                "llm": self.llm_config.public_dict(),
                "telegram": self.telegram.public_dict() if self.telegram else {},
            }
        )

    async def start_goal(self, goal: str) -> None:
        clean_goal = (goal or "").strip()
        if not clean_goal:
            return

        if self._task and not self._task.done():
            await self.stop_goal(silent=True)

        self.current_goal = clean_goal
        self.current_plan = [{"step": "Memahami perintah & menyiapkan langkah...", "status": "in_progress"}]
        self.history = []
        self.memory_notes = dict(self.persistent_notes)
        self.pending_approval = None
        self.takeover_reason = ""
        self.manual_takeover = False
        self._stop_requested = False
        self._form_touched = False
        self._expected_fields = {}
        self._field_warnings = {}
        self._last_submit_verification = None
        self._verify_retry_used = False
        self._pre_submit_blocked_once = False

        await self._push_chat("user", clean_goal)
        self.state = "RUNNING"
        self.live_status = "Sedang memahami perintah..."
        await self._sync_state()

        self._task = asyncio.create_task(self._run_loop())

    async def stop_goal(self, silent: bool = False) -> None:
        self._stop_requested = True
        self.pending_approval = None
        self.takeover_reason = ""
        self._approval_decision = "reject"
        self._resume_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.state = "IDLE"
        self.live_status = "Online · Siap disuruh apa aja"
        await self._sync_state()
        if not silent:
            await self._push_chat("system", "Pekerjaan dihentikan.")

    async def toggle_manual_takeover(self, enabled: bool) -> None:
        self.manual_takeover = enabled
        if enabled and self.state == "RUNNING":
            self.state = "WAITING_HUMAN_TAKEOVER"
            self.live_status = "Layar sedang dikontrol manual oleh lu"
            self.takeover_reason = "Silakan operasikan layar browser. Tap 'Udah Beres — Lanjut!' kalau mau diterusin."
            self._resume_event.clear()
        elif not enabled and self.state == "WAITING_HUMAN_TAKEOVER":
            self.takeover_reason = ""
            self._resume_event.set()
        await self._sync_state()

    async def resume_from_takeover(self) -> None:
        self.manual_takeover = False
        self.takeover_reason = ""
        if self.state == "WAITING_HUMAN_TAKEOVER":
            await self._push_chat("system", "✅ Oke sip! Gua lanjutin lagi kerjanya...")
            self.state = "RUNNING"
            self.live_status = "Melanjutkan pekerjaan..."
            self._resume_event.set()
        await self._sync_state()
        await self.browser.refresh_frame()

    async def resolve_approval(self, decision: str) -> None:
        if self.state != "WAITING_APPROVAL":
            return
        self._approval_decision = decision
        self._resume_event.set()

    # ======================== MAIN MULTI-STEP AGENT LOOP ========================

    async def _run_loop(self) -> None:
        try:
            for step_num in range(1, self.MAX_STEPS + 1):
                if self._stop_requested:
                    break

                if self.state == "WAITING_HUMAN_TAKEOVER" or self.manual_takeover:
                    self.state = "WAITING_HUMAN_TAKEOVER"
                    self.live_status = "Nunggu lu selesai di layar..."
                    await self._sync_state()
                    await self._resume_event.wait()
                    if self._stop_requested:
                        break
                    self.state = "RUNNING"
                    await self._sync_state()

                self.live_status = f"Langkah {step_num}: Membaca tampilan website..."
                await self._sync_state()

                decision = None
                dom_state = None
                last_step_err = None
                for _attempt in range(2):
                    try:
                        dom_state = await self.browser.observe(annotated_screenshot=self.llm_config.has_api_key)
                        ws_files = self.file_manager.list_files()
                        decision = await self.brain.decide_next_step(
                            goal=self.current_goal,
                            dom_state=dom_state,
                            history=self.history,
                            workspace_files=ws_files,
                            memory_notes=self.memory_notes,
                            recent_chat=self.chat_messages[:-1],
                        )
                        break
                    except Exception as _dec_exc:
                        last_step_err = _dec_exc
                        try:
                            await self.browser.ensure_alive()
                        except Exception:
                            pass
                        await asyncio.sleep(1.0)

                if decision is None or dom_state is None:
                    err_detail = (
                        f"{type(last_step_err).__name__}: {last_step_err}".strip(": ")
                        if last_step_err is not None
                        else "Gangguan pembacaan langkah"
                    )
                    raise RuntimeError(err_detail)

                thought = decision.get("thought", "")
                if decision.get("plan") and isinstance(decision["plan"], list):
                    self.current_plan = decision["plan"]

                action = decision.get("action") or {"name": "done", "summary": "Selesai."}
                act_name = (action.get("name") or "done").lower()

                # SELESAI? (Gerbang Verifikasi Ketat Pasca-Submit — Ketentuan #7, #8, #9!)
                if act_name == "done":
                    goal_low = self.current_goal.lower()
                    wants_submit = any(k in goal_low for k in ("submit", "kirim", "daftar", "simpan form", "isi form", "formulir", "practice-form"))
                    if self._form_touched and wants_submit:
                        # Jika belum pernah klik Submit padahal diminta submit -> tolak klaim selesai prematur!
                        if self._last_submit_verification is None and step_num <= 23:
                            self.history.append({
                                "step": step_num,
                                "action_name": "verify_gate",
                                "result": "⚠️ DITOLAK SELESAI PREMATUR: Formulir belum di-Submit dan tabel hasil belum diverifikasi! Periksa semua field terhadap instruksi user, klik tombol Submit, lalu verifikasi tabel hasilnya.",
                            })
                            continue

                        # Jika hasil verifikasi tabel pasca-submit menemukan ketidaksesuaian / gagal!
                        if self._last_submit_verification and not self._last_submit_verification.get("ok", False):
                            problems_list = self._last_submit_verification.get("problems", [])
                            if not self._verify_retry_used and step_num <= 22:
                                self._verify_retry_used = True
                                self.history.append({
                                    "step": step_num,
                                    "action_name": "verify_gate",
                                    "result": "❌ VERIFIKASI TABEL HASIL GAGAL — Dilarang melapor berhasil! Perbaiki field bermasalah berikut lalu submit ulang:\n- " + "\n- ".join(problems_list),
                                })
                                continue

                            # Jika tetap gagal setelah percobaan perbaikan -> WAJIB lapor GAGAL jujur beserta nama field & penyebabnya!
                            for p in self.current_plan:
                                if p.get("status") == "in_progress":
                                    p["status"] = "pending"
                            self.state = "IDLE"
                            self.live_status = "Verifikasi formulir menemukan ketidaksesuaian"
                            await self._sync_state()
                            fail_lines = "\n".join(f"• {item}" for item in problems_list) or "• Tabel hasil submit tidak muncul atau data tidak sesuai instruksi."
                            honest_fail_msg = (
                                "❌ Pengisian Formulir GAGAL Verifikasi (Data Belum Sesuai)!\n\n"
                                "Daftar field yang bermasalah beserta penyebabnya:\n"
                                f"{fail_lines}"
                            )
                            await self._push_chat("agent", honest_fail_msg)
                            if self.telegram:
                                await self.telegram.notify_done(honest_fail_msg)
                            return

                    for p in self.current_plan:
                        if p.get("status") == "in_progress":
                            p["status"] = "completed"
                    self.state = "IDLE"
                    self.live_status = "Online · Siap disuruh apa aja"
                    await self._sync_state()
                    summary_msg = action.get("summary") or thought or "Pekerjaan beres dijalankan!"
                    await self._push_chat("agent", summary_msg)
                    if self.telegram:
                        await self.telegram.notify_done(summary_msg)
                    return

                # HUMAN LOGIN / CAPTCHA / 2FA TAKEOVER?
                if act_name == "request_human_login":
                    goal_l = self.current_goal.lower()
                    has_explicit_creds_in_goal = (
                        any(k in goal_l for k in ("password:", "password ", "pass:", "sandi:", "secret_sauce"))
                        and not any(h.get("action_name") == "auto_cred_hint" for h in self.history)
                    )
                    if has_explicit_creds_in_goal:
                        self.history.append({
                            "step": step_num,
                            "action_name": "auto_cred_hint",
                            "result": "User SUDAH menuliskan Username & Password secara eksplisit di dalam instruksinya! Jangan panggil request_human_login — langsung isikan dengan action 'type' lalu klik tombol Login.",
                        })
                        continue
                    reason = action.get("reason") or "Halaman ini butuh login akun. Tinggal balas 'username | password' langsung di chat ini!"
                    await self._wait_for_human_takeover(reason)
                    self.history.append({"step": step_num, "action_name": "request_human_login", "result": "User selesai login."})
                    continue

                self.live_status = thought[:65] if thought else f"Menjalankan {act_name}..."
                await self._sync_state()

                if thought:
                    await self._push_chat("step", thought, {"step": step_num, "action": act_name})
                    if self.telegram:
                        await self.telegram.notify_step(step_num, thought, self.current_plan)

                try:
                    result_text = await self._execute_with_guardrails(step_num, action, dom_state)
                except Exception as step_exc:
                    err_label = str(step_exc).strip() or type(step_exc).__name__
                    try:
                        await self.browser.ensure_alive()
                    except Exception:
                        pass
                    result_text = f"Kendala ringan pada aksi {act_name} ({err_label[:80]}), halaman sudah disegarkan."
                self.history.append({"step": step_num, "action_name": act_name, "result": result_text})

                # Jika situs utama yang diminta user memblokir IP Cloud / down (misal portal .go.id tertentu),
                # langsung hentikan dalam 9 detik dan beri penjelasan jernih tanpa muter berulang-ulang!
                if isinstance(result_text, str) and result_text.startswith("[UNREACHABLE_SITE]"):
                    from urllib.parse import urlparse as _up
                    req_host = _up(action.get("url") or "").netloc.lower()
                    if req_host and req_host in self.current_goal.lower():
                        self.state = "IDLE"
                        self.live_status = "Situs tujuan menolak koneksi Cloud"
                        await self._sync_state()
                        px_hint = (
                            "Pasang Proxy Indonesia lewat ketik `/proxy` kalau mau tembus situs `.go.id` ini, "
                            "atau lanjut ke situs lain!"
                            if not self.browser.proxy_config
                            else "Cek status `/proxy` untuk memastikan kuota/koneksi Proxy Indonesia aktif!"
                        )
                        clean_unreach = (
                            f"🚫 Situs `{req_host}` menolak atau tidak merespons koneksi dalam 9 detik.\n\n"
                            "Ini biasa terjadi pada portal pemerintah (`.go.id`) yang dikunci khusus jaringan provider lokal Indonesia (Telkomsel/IndiHome/Biznet). "
                            f"{px_hint}"
                        )
                        await self._push_chat("agent", clean_unreach)
                        if self.telegram:
                            await self.telegram.notify_done(clean_unreach)
                        return

                await asyncio.sleep(0.08)

            self.state = "IDLE"
            self.live_status = "Online · Siap disuruh apa aja"
            await self._sync_state()
            max_msg = "Udah jalan maksimal 26 langkah bro. Kalau masih mau dilanjutin tinggal balas aja!"
            await self._push_chat("agent", max_msg)
            if self.telegram:
                await self.telegram.notify_done(max_msg)

        except asyncio.CancelledError:
            self.state = "IDLE"
            self.live_status = "Online · Siap disuruh apa aja"
            await self._sync_state()
        except Exception as exc:
            self.state = "IDLE"
            self.live_status = "Online · Siap disuruh apa aja"
            await self._sync_state()
            # DILARANG memanggil quick_chat di sini karena quick_chat tidak melihat browser dan bisa mengarang klaim palsu!
            reason_txt = str(exc).strip() or f"Gangguan koneksi sementara ({type(exc).__name__})"
            cur_u = ""
            try:
                if self.browser.page:
                    cur_u = self.browser._display_url(self.browser.page.url)
            except Exception:
                pass
            loc_info = f" di halaman `{cur_u}`" if cur_u and cur_u != "about:blank" else ""
            err_msg = (
                f"⚠️ Langkah pengerjaan web sempat terputus{loc_info} ({reason_txt[:110]}).\n\n"
                "Posisi halaman terakhir masih tersimpan — ketik `lanjut` kalau mau gua terusin dari halaman itu!"
            )
            await self._push_chat("agent", err_msg)
            if self.telegram:
                await self.telegram.send_to_all(err_msg)

    async def _wait_for_human_takeover(self, reason: str) -> None:
        self.state = "WAITING_HUMAN_TAKEOVER"
        self.live_status = "🔐 Nunggu lu login dulu..."
        self.manual_takeover = True
        self.takeover_reason = reason
        self._resume_event.clear()
        await self._sync_state()

        await self._push_chat("takeover", reason, {"reason": reason})
        if self.telegram:
            await self.telegram.notify_takeover(reason, self.browser.latest_frame_b64)

        await self._resume_event.wait()
        self.manual_takeover = False
        self.takeover_reason = ""
        self.state = "RUNNING"
        await self._sync_state()

    async def _execute_with_guardrails(
        self,
        step_num: int,
        action: Dict[str, Any],
        dom_state: Dict[str, Any],
    ) -> str:
        act_name = (action.get("name") or "").lower()
        elements = dom_state.get("elements", [])

        if act_name == "type":
            idx = int(action.get("index", -1))
            target_el = next((e for e in elements if e["index"] == idx), None)
            if is_credential_element(target_el):
                typed_val = str(action.get("text", "")).strip()
                # Izinkan isi otomatis HANYA bila nilai password tersebut memang ditulis langsung oleh user di dalam perintahnya!
                is_user_supplied_in_prompt = (
                    bool(typed_val)
                    and len(typed_val) >= 3
                    and typed_val.lower() in self.current_goal.lower()
                )
                if not is_user_supplied_in_prompt:
                    await self._wait_for_human_takeover(
                        "Ketemu kolom Password/OTP. Balas langsung di chat 'username | password' (otomatis dihapus 1 detik) atau isi di form simpel!"
                    )
                    return "User mengisi kredensial sendiri."

        if act_name in ("click", "ask_approval"):
            idx = int(action.get("index", -1))
            target_el = next((e for e in elements if e["index"] == idx), None)
            sens_report = inspect_element_sensitivity(target_el)

            if act_name == "ask_approval" and not sens_report:
                sens_report = {
                    "category": "Persetujuan Tindakan Sensitif",
                    "target_label": (target_el or {}).get("text") or f"Tombol [{idx}]",
                    "reason": action.get("reason") or "Butuh persetujuan lu sebelum tombol ini diklik.",
                    "rect": (target_el or {}).get("rect"),
                }

            if sens_report:
                approved = await self._prompt_user_approval(sens_report)
                if approved == "reject":
                    await self._push_chat("system", f"🚫 Oke, `{sens_report['target_label']}` dibatalkan.")
                    return f"Tindakan '{sens_report['target_label']}' DITOLAK oleh user. Jangan ulangi klik ini."
                if approved == "takeover":
                    await self._wait_for_human_takeover(
                        f"Silakan cek & klik sendiri tombol '{sens_report['target_label']}' di layar."
                    )
                    return f"User mengambil alih layar pada tahap '{sens_report['target_label']}'."
                await self._push_chat("system", f"✅ Disetujui! Mengeksekusi `{sens_report['target_label']}`...")
                if idx > 0:
                    return await self.browser.click_element(idx, elements)
                return "Disetujui oleh user."

        if act_name == "navigate":
            return await self.browser.navigate(action.get("url", ""))

        if act_name == "click":
            idx = int(action.get("index", 0))
            target_el = next((e for e in elements if e["index"] == idx), None)
            if self._is_submit_target(target_el):
                return await self._handle_form_submit_click(idx, target_el, elements)

            res = await self.browser.click_element(idx, elements)
            if target_el and (target_el.get("type") or "").lower() in ("radio", "checkbox"):
                self._form_touched = True
                lbl = (target_el.get("label") or target_el.get("name") or "").strip()
                if lbl and "CHECKED=True" in res:
                    grp = "Gender" if "gender" in (target_el.get("el_id", "") + target_el.get("name", "")).lower() else (
                        "Hobbies" if "hobbies" in (target_el.get("el_id", "") + target_el.get("name", "")).lower() else lbl
                    )
                    if grp == "Hobbies" and self._expected_fields.get("Hobbies"):
                        prev_h = [x.strip() for x in self._expected_fields["Hobbies"].split(",") if x.strip()]
                        if lbl not in prev_h:
                            prev_h.append(lbl)
                        self._expected_fields["Hobbies"] = ", ".join(prev_h)
                    else:
                        self._expected_fields[grp] = lbl
            return res

        if act_name == "type":
            idx = int(action.get("index", 0))
            val_txt = str(action.get("text", ""))
            target_el = next((e for e in elements if e["index"] == idx), None)
            if target_el and (target_el.get("el_id") or target_el.get("name") or target_el.get("label")):
                self._form_touched = True
            res = await self.browser.type_into_element(
                index=idx,
                text=val_txt,
                elements=elements,
                press_enter=bool(action.get("press_enter", False)),
            )
            if target_el:
                fkey = (target_el.get("label") or target_el.get("placeholder") or target_el.get("name") or target_el.get("el_id") or f"field_{idx}").strip()
                if res.startswith("❌"):
                    self._field_warnings[fkey] = res
                else:
                    self._field_warnings.pop(fkey, None)
                    self._expected_fields[fkey] = val_txt
            return res

        if act_name == "select":
            idx = int(action.get("index", 0))
            opt_txt = str(action.get("option", ""))
            target_el = next((e for e in elements if e["index"] == idx), None)
            self._form_touched = True
            res = await self.browser.select_dropdown(
                index=idx,
                option_value_or_text=opt_txt,
                elements=elements,
            )
            if target_el:
                fkey = (target_el.get("label") or target_el.get("name") or target_el.get("el_id") or f"select_{idx}").strip()
                if res.startswith("❌"):
                    self._field_warnings[fkey] = res
                else:
                    self._field_warnings.pop(fkey, None)
                    self._expected_fields[fkey] = opt_txt
            return res

        if act_name == "scroll":
            return await self.browser.scroll_page(direction=str(action.get("direction", "down")))

        if act_name == "go_back":
            return await self.browser.go_back()

        if act_name == "solve_captcha":
            cap_res = await self.browser.auto_solve_captcha(solver_fn=self.brain.solve_captcha_image)
            return cap_res or "Tidak ditemukan CAPTCHA gambar/hitungan yang perlu diisi di halaman ini."

        if act_name == "list_files":
            flist = self.file_manager.list_files()
            summary = ", ".join(f["name"] for f in flist) or "(kosong)"
            self.memory_notes["workspace_files"] = summary
            return f"Daftar file: {summary}"

        if act_name == "read_file":
            fname = str(action.get("filename", ""))
            content = self.file_manager.read_file(fname)
            self.memory_notes[fname] = content
            return f"Berhasil membaca file '{fname}'."

        if act_name in ("write_file", "append_file"):
            fname = str(action.get("filename", "hasil.md"))
            content = str(action.get("content", ""))
            saved = (
                self.file_manager.write_file(fname, content)
                if act_name == "write_file"
                else self.file_manager.append_file(fname, content)
            )
            await self.broadcast({"type": "files_updated", "files": self.file_manager.list_files()})
            await self._push_chat(
                "file",
                f"File siap: {saved['name']}",
                {"filename": saved["name"], "size": saved["size"]},
            )
            if self.telegram:
                fpath = self.file_manager.get_file_path(saved["name"])
                await self.telegram.notify_file(saved["name"], fpath.read_bytes())
            return f"File '{saved['name']}' berhasil disimpan ({saved['size']} bytes)."

        return f"Action '{act_name}' tidak dikenali."

    async def _prompt_user_approval(self, sens_report: Dict[str, Any]) -> str:
        self.state = "WAITING_APPROVAL"
        self.live_status = "🛡️ Menunggu persetujuan lu..."
        self.pending_approval = sens_report
        self._approval_decision = None
        self._resume_event.clear()

        await self._sync_state()
        await self._push_chat(
            "approval",
            f"Mau klik tombol **{sens_report['target_label']}**. Boleh dilanjutin bro?",
            sens_report,
        )
        if self.telegram:
            await self.telegram.notify_approval(sens_report, self.browser.latest_frame_b64)

        await self._resume_event.wait()

        decision = self._approval_decision or "reject"
        self.pending_approval = None
        self.state = "RUNNING"
        await self._sync_state()
        return decision

    # ======================== FORM PRE-SUBMIT & POST-SUBMIT VERIFICATION ========================

    @staticmethod
    def _is_submit_target(target_el: Optional[Dict[str, Any]]) -> bool:
        if not target_el:
            return False
        etype = (target_el.get("type") or "").lower()
        eid = (target_el.get("el_id") or "").lower()
        name_attr = (target_el.get("name") or "").lower()
        txt = (target_el.get("text") or target_el.get("label") or "").strip().lower()
        combined = f"{eid} {name_attr} {txt}"
        # Jangan anggap tombol Login, Continue, Next, Search, atau Add sebagai Submit akhir formulir!
        if any(skip in combined for skip in ("login", "log in", "signin", "sign in", "masuk", "continue", "next", "lanjut", "search", "cari", "cancel", "batal", "close", "add-to-cart", "remove")):
            return False
        if eid == "submit" or txt in ("submit", "kirim", "daftar", "simpan"):
            return True
        if etype == "submit" and txt not in ("", "go"):
            return True
        return False

    async def _handle_form_submit_click(
        self,
        idx: int,
        target_el: Optional[Dict[str, Any]],
        elements: List[Dict[str, Any]],
    ) -> str:
        """
        1. Validasi seluruh isi form terhadap format, kelengkapan, & instruksi user SEBELUM klik Submit (Ketentuan #5 & #6).
        2. Klik Submit lewat DOM selector, baca tabel hasil submit, dan bandingkan baris demi baris (Ketentuan #7, #8, #9).
        """
        import re as _re

        self._form_touched = True
        audit = await self.browser.audit_form_state()
        pre_issues: List[str] = []

        for iss in audit.get("issues", []):
            pre_issues.append(f"{iss.get('field', 'Field')}: {iss.get('reason', 'belum sesuai')}")

        for warn_f, warn_msg in self._field_warnings.items():
            clean_w = warn_msg.replace("❌ ", "")
            pre_issues.append(f"{warn_f}: {clean_w}")

        # Cek khusus jika user meminta mengisi Subjects namun chip Subjects masih kosong
        goal_low = self.current_goal.lower()
        if "subject" in goal_low:
            subj_field = next((f for f in audit.get("fields", []) if "subject" in (f.get("id", "") + f.get("field", "")).lower()), None)
            if subj_field and not subj_field.get("value"):
                pre_issues.append("Subjects: diminta oleh pengguna tetapi belum ada opsi autocomplete yang terpilih (masih kosong)")

        if pre_issues and not self._pre_submit_blocked_once:
            self._pre_submit_blocked_once = True
            current_dump = ", ".join(
                f"{f.get('field')}='{f.get('value')}'"
                for f in audit.get("fields", [])
                if f.get("value") not in ("", "Unchecked")
            )
            return (
                "❌ TAHAN SUBMIT (Validasi Pra-Submit Menemukan Data Belum Sesuai):\n- "
                + "\n- ".join(pre_issues)
                + f"\nData terisi saat ini: [{current_dump}]. Perbaiki field yang bermasalah terlebih dahulu sebelum klik Submit!"
            )

        url_before = self.browser.page.url if self.browser.page else ""
        click_msg = await self.browser.click_element(idx, elements)
        await asyncio.sleep(0.45)
        url_after = self.browser.page.url if self.browser.page else ""

        sub_res = await self.browser.read_submission_result()
        problems: List[str] = []

        # Masukkan peringatan format/panjang yang belum diperbaiki
        for warn_f, warn_msg in self._field_warnings.items():
            problems.append(f"Field '{warn_f}': {warn_msg.replace('❌ ', '')}")

        if not sub_res.get("submitted"):
            # Jika tidak ada field :invalid DAN halaman sudah berpindah URL (mis. halaman konfirmasi non-tabel) atau modal sudah tertutup bersih
            has_invalids = bool(sub_res.get("invalid_fields")) or bool(audit.get("issues"))
            if not has_invalids and not problems and (url_after != url_before or "tabel" not in goal_low):
                self._last_submit_verification = {"ok": True, "rows": [], "problems": []}
                return f"{click_msg} -> ✅ Form berhasil dikirim (tidak ada error validasi)."

            for inv in sub_res.get("invalid_fields", []):
                problems.append(
                    f"Field '{inv.get('field')}': nilai '{inv.get('value')}' ditolak ({inv.get('reason')})"
                )
            for iss in audit.get("issues", []):
                msg_cand = f"Field '{iss.get('field')}': {iss.get('reason')}"
                if msg_cand not in problems:
                    problems.append(msg_cand)
            if not problems:
                problems.append("Tabel hasil submit tidak muncul (kemungkinan ada field wajib/format yang belum terpenuhi).")

            self._last_submit_verification = {"ok": False, "rows": [], "problems": problems}
            return (
                f"{click_msg} -> ❌ SUBMIT GAGAL (Tabel hasil belum muncul)!\n"
                "Field bermasalah:\n- " + "\n- ".join(problems)
            )

        # Bandingkan baris Tabel Hasil Submit dengan data yang diharapkan (Ketentuan #7, #8, #9)
        rows: List[Dict[str, str]] = sub_res.get("rows", [])
        table_map = {r["label"].lower(): r["value"] for r in rows}

        def _find_table_val(field_key: str) -> Optional[Tuple[str, str]]:
            fk = field_key.lower()
            alias_pairs = [
                (("first name", "last name", "nama depan", "nama belakang", "fullname", "student name"), "student name"),
                (("email", "useremail", "student email"), "student email"),
                (("gender", "jenis kelamin"), "gender"),
                (("mobile", "usernumber", "phone", "telepon", "nomor"), "mobile"),
                (("subject", "subjects", "mata pelajaran"), "subjects"),
                (("hobbi", "hobbies", "hobi"), "hobbies"),
                (("address", "currentaddress", "alamat"), "address"),
                (("state", "city", "kota", "provinsi"), "state and city"),
                (("birth", "date", "tanggal lahir"), "date of birth"),
            ]
            for keys, tbl_lbl in alias_pairs:
                if any(k in fk for k in keys) and tbl_lbl in table_map:
                    return tbl_lbl, table_map[tbl_lbl]
            for r in rows:
                rl = r["label"].lower()
                if fk in rl or rl in fk:
                    return r["label"], r["value"]
            return None

        for exp_k, exp_v in self._expected_fields.items():
            if not exp_v or "date" in exp_k.lower() or "birth" in exp_k.lower():
                continue
            matched_row = _find_table_val(exp_k)
            if matched_row is None:
                continue
            tbl_label, tbl_val = matched_row
            if not tbl_val.strip():
                problems.append(
                    f"Field '{tbl_label}' ({exp_k}): kosong di tabel hasil, padahal diminta '{exp_v}' (kemungkinan opsi autocomplete/pilihan tidak terklik)"
                )
                continue
            # Cek setiap item (mis. multi-subject dipisah koma) ada di dalam nilai tabel hasil
            sub_parts = [p.strip() for p in _re.split(r"[,;]+", exp_v) if p.strip()]
            for sp in sub_parts:
                if sp.lower() not in tbl_val.lower():
                    problems.append(
                        f"Field '{tbl_label}' ({exp_k}): tertulis '{tbl_val}' di tabel hasil, tidak sesuai dengan yang diminta '{exp_v}'"
                    )
                    break

        if "subject" in goal_low and "subjects" in table_map and not table_map["subjects"].strip():
            problems.append("Field 'Subjects': kosong di tabel hasil karena opsi autocomplete belum terpilih")

        table_lines = [f"  • {r['label']}: {r['value'] or '(kosong)'}" for r in rows]
        table_str = "\n".join(table_lines)

        if problems:
            self._last_submit_verification = {"ok": False, "rows": rows, "problems": problems}
            return (
                f"{click_msg}\n📋 Tabel Hasil Terbaca:\n{table_str}\n\n"
                "❌ VERIFIKASI PASCA-SUBMIT GAGAL! Ditemukan ketidaksesuaian:\n- " + "\n- ".join(problems)
            )

        self._last_submit_verification = {"ok": True, "rows": rows, "problems": []}
        return (
            f"{click_msg}\n✅ VERIFIKASI TABEL HASIL SESUAI 100%!\n"
            f"📋 Data Terverifikasi pada Tabel Hasil ({sub_res.get('modal_title') or 'Submitted'}):\n{table_str}"
        )
