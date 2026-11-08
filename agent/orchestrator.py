import asyncio
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional

from agent.browser_engine import CloudBrowserEngine
from agent.file_manager import WorkspaceFileManager
from agent.guardrails import inspect_element_sensitivity, is_credential_element
from agent.llm_providers import LLMConfig, MultiLLMBrain
from agent.telegram_bridge import TelegramBotBridge


class AgentOrchestrator:
    """
    Otak orkestrasi multi-tahap dengan antarmuka gaya Chat WA/Telegram:
    Browser berjalan tenang di belakang layar, dan HANYA memunculkan layar
    secara otomatis ketika butuh Login user atau Persetujuan Tindakan Sensitif.
    """

    MAX_STEPS = 18

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

                thought = decision.get("thought", "")
                if decision.get("plan") and isinstance(decision["plan"], list):
                    self.current_plan = decision["plan"]

                action = decision.get("action") or {"name": "done", "summary": "Selesai."}
                act_name = (action.get("name") or "done").lower()

                # SELESAI?
                if act_name == "done":
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
                    reason = action.get("reason") or "Halaman ini butuh login akun. Silakan masuk sendiri biar password lu aman."
                    await self._wait_for_human_takeover(reason)
                    self.history.append({"step": step_num, "action_name": "request_human_login", "result": "User selesai login."})
                    continue

                self.live_status = thought[:65] if thought else f"Menjalankan {act_name}..."
                await self._sync_state()

                if thought:
                    await self._push_chat("step", thought, {"step": step_num, "action": act_name})
                    if self.telegram:
                        await self.telegram.notify_step(step_num, thought)

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

                await asyncio.sleep(0.08)

            self.state = "IDLE"
            self.live_status = "Online · Siap disuruh apa aja"
            await self._sync_state()
            max_msg = "Udah jalan maksimal 18 langkah bro. Kalau masih mau dilanjutin tinggal balas aja!"
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
            # Self-healing fallback: jika browser crash/berat, langsung jawab pakai jalur kilat AI + data live agar user TIDAK PERNAH dapat pesan error buntu!
            try:
                asyncio.create_task(self.browser._force_restart())
            except Exception:
                pass
            try:
                fallback_reply = await self.brain.quick_chat(self.current_goal, self.chat_messages[:-1])
                if fallback_reply and not fallback_reply.startswith("Waduh koneksi"):
                    await self._push_chat("agent", fallback_reply)
                    if self.telegram:
                        await self.telegram.notify_done(fallback_reply)
                    return
            except Exception:
                pass
            reason_txt = str(exc).strip() or f"Halaman web terlalu berat ({type(exc).__name__})"
            err_msg = f"Halaman web barusan terlalu berat di memori server ({reason_txt[:100]}), browser otomatis gua segarin ulang ya bro!"
            await self._push_chat("agent", err_msg)
            if self.telegram:
                await self.telegram.send_to_all(f"🔄 {err_msg}")

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
                await self._wait_for_human_takeover(
                    "Ketemu kolom Password/OTP. Silakan ketik sendiri di layar biar rahasia akun lu aman."
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
            return await self.browser.click_element(int(action.get("index", 0)), elements)

        if act_name == "type":
            return await self.browser.type_into_element(
                index=int(action.get("index", 0)),
                text=str(action.get("text", "")),
                elements=elements,
                press_enter=bool(action.get("press_enter", False)),
            )

        if act_name == "select":
            return await self.browser.select_dropdown(
                index=int(action.get("index", 0)),
                option_value_or_text=str(action.get("option", "")),
                elements=elements,
            )

        if act_name == "scroll":
            return await self.browser.scroll_page(direction=str(action.get("direction", "down")))

        if act_name == "go_back":
            return await self.browser.go_back()

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
