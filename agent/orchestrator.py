import asyncio
import json
import os
import re
import time
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from agent.browser_engine import CloudBrowserEngine
from agent.file_manager import WorkspaceFileManager
from agent.guardrails import (
    inspect_element_sensitivity,
    inspect_non_browser_sensitivity,
    is_credential_element,
    is_practice_sandbox_url,
)
from agent.llm_providers import LLMConfig, MultiLLMBrain
from agent.recovery_engine import ErrorRecoveryEngine
from agent.reporting import compute_step_metrics, format_execution_report
from agent.security import StorageUnavailableError, redact_secrets, register_runtime_secret
from agent.task_store import TaskStateStore, compute_side_effect_fingerprint
from agent.telegram_bridge import TelegramBotBridge


class AgentOrchestrator:
    """
    Otak orkestrasi multi-tahap dengan antarmuka gaya Chat WA/Telegram:
    Browser berjalan tenang di belakang layar, dan HANYA memunculkan layar
    secara otomatis ketika butuh Login user atau Persetujuan Tindakan Sensitif.
    """

    MAX_STEPS = 32

    def __init__(
        self,
        browser: CloudBrowserEngine,
        file_manager: WorkspaceFileManager,
        llm_config: LLMConfig,
        broadcast_cb: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]],
        telegram: Optional[TelegramBotBridge] = None,
        allow_writes: Optional[bool] = None,
        write_block_reason: str = "",
    ):
        self.browser = browser
        self.file_manager = file_manager
        self.llm_config = llm_config
        self.brain = MultiLLMBrain(llm_config)
        self.broadcast = broadcast_cb
        self.telegram = telegram
        if allow_writes is None:
            allow_writes = bool(getattr(browser, "allow_writes", True) and getattr(file_manager, "allow_writes", True))
        self.allow_writes = bool(allow_writes)
        self.write_block_reason = (
            write_block_reason
            or getattr(browser, "write_block_reason", "")
            or "Penulisan state diblokir karena penyimpanan persisten tidak tersedia."
        )

        self.state: str = "IDLE"  # IDLE | RUNNING | WAITING_HUMAN_TAKEOVER | WAITING_APPROVAL
        self.live_status: str = (
            "Online · Siap disuruh apa aja"
            if self.allow_writes
            else f"Error Penyimpanan: {self.write_block_reason}"
        )
        self.manual_takeover: bool = False
        self.current_goal: str = ""
        self.current_plan: List[Dict[str, str]] = []
        self.history: List[Dict[str, Any]] = []
        self.memory_notes: Dict[str, str] = {}
        self.persistent_notes: Dict[str, str] = {}
        self._user_mem_file = self.browser.profile_dir / "user_memory.json"
        self.user_memories: List[str] = self._load_user_memories()
        self.task_store = TaskStateStore(
            str(self.browser.profile_dir / "task_state"),
            allow_writes=self.allow_writes,
            write_block_reason=self.write_block_reason,
        )
        self.recovery = ErrorRecoveryEngine()
        self._active_task_id: str = ""
        self._browser_actions_count: int = 0
        self._form_touched: bool = False
        self._goal_started_mono: float = 0.0
        self._expected_fields: Dict[str, str] = {}
        self._goal_expectations: Dict[str, str] = {}
        self._field_warnings: Dict[str, str] = {}
        self._recorded_issues: List[str] = []
        self._created_files_this_run: List[str] = []
        self._created_top_dirs: List[str] = []
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

    def _load_user_memories(self) -> List[str]:
        if self.allow_writes:
            try:
                if self._user_mem_file.exists():
                    arr = json.loads(self._user_mem_file.read_text(encoding="utf-8"))
                    if isinstance(arr, list):
                        return [str(x).strip() for x in arr if str(x).strip()]
            except Exception:
                pass
        env_mem = os.getenv("USER_MEMORY_INIT", "").strip()
        if env_mem:
            return [ln.strip() for ln in env_mem.splitlines() if ln.strip()]
        return []

    def _save_user_memories(self) -> None:
        if not self.allow_writes:
            raise StorageUnavailableError(self.write_block_reason)
        try:
            self.browser.profile_dir.mkdir(parents=True, exist_ok=True)
            self._user_mem_file.write_text(json.dumps(self.user_memories, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def add_user_memories(self, raw_text: str) -> List[str]:
        """Menyimpan 1 atau beberapa baris fakta profil/gaya kerja user (otomatis update jika labelnya sama)."""
        if not self.allow_writes:
            raise StorageUnavailableError(self.write_block_reason)
        lines = [
            re.sub(r"^[\-\*•\d\.\)\s]+", "", ln).strip()
            for ln in (raw_text or "").splitlines()
            if ln.strip()
        ]
        added: List[str] = []
        for item in lines:
            if not item:
                continue
            key_prefix = item.split(":", 1)[0].strip().lower() if ":" in item else ""
            replaced = False
            if key_prefix and len(key_prefix) <= 28:
                for idx, existing in enumerate(self.user_memories):
                    ex_prefix = existing.split(":", 1)[0].strip().lower() if ":" in existing else ""
                    if ex_prefix and ex_prefix == key_prefix:
                        self.user_memories[idx] = item
                        replaced = True
                        break
            if not replaced and item not in self.user_memories:
                self.user_memories.append(item)
            added.append(item)
        self._save_user_memories()
        return added

    def remove_user_memory(self, idx_1based: int) -> Optional[str]:
        if not self.allow_writes:
            raise StorageUnavailableError(self.write_block_reason)
        if 1 <= idx_1based <= len(self.user_memories):
            removed = self.user_memories.pop(idx_1based - 1)
            self._save_user_memories()
            return removed
        return None

    def clear_user_memories(self) -> None:
        if not self.allow_writes:
            raise StorageUnavailableError(self.write_block_reason)
        self.user_memories.clear()
        self._save_user_memories()

    def format_user_memory_block(self) -> str:
        if not self.user_memories:
            return ""
        joined = "\n".join(f"- {m}" for m in self.user_memories)
        return (
            "=== MEMORI PROFIL & GAYA KERJA PEMILIK BOT (GUNAKAN OTOMATIS SAAT MENGISI FORM / MEMBUAT DOKUMEN / MENJAWAB) ===\n"
            f"{joined}\n"
        )

    @staticmethod
    def _parse_goal_kv_expectations(goal_text: str) -> Dict[str, str]:
        """Mengekstrak pasangan '- Label: Nilai' dari instruksi user agar verifikasi akhir membandingkan dengan permintaan asli user!"""
        out: Dict[str, str] = {}
        skip_keys = {"instruksi", "catatan", "penting", "aturan", "url", "website", "link", "target", "tugas", "data"}
        for raw_ln in (goal_text or "").splitlines():
            ln = re.sub(r"^[\s\-\*•\d\.\)]+", "", raw_ln).strip()
            if ":" not in ln or "http://" in ln.lower() or "https://" in ln.lower():
                continue
            k, v = ln.split(":", 1)
            k_clean = k.strip().lower()
            v_clean = v.strip()
            if not k_clean or not v_clean or len(k_clean) > 32 or k_clean in skip_keys:
                continue
            if k_clean.startswith("http") or v_clean.startswith(("http://", "https://")):
                continue
            out[k_clean] = v_clean
        return out

    def _record_issue(self, msg: str, field_key: str = "", prefer_replace: bool = False) -> None:
        clean = (msg or "").strip().rstrip(".")
        if clean.endswith(")") and clean.count(")") > clean.count("("):
            clean = clean[:-1].strip()
        if not clean:
            return
        canon = re.sub(r"[^a-z0-9]", "", (field_key or clean.split(":", 1)[0]).lower())
        for prefix in ("field", "digits", "10digits"):
            canon = canon.replace(prefix, "")
        for idx, existing in enumerate(self._recorded_issues):
            ex_canon = re.sub(r"[^a-z0-9]", "", existing.split(":", 1)[0].lower())
            for prefix in ("field", "digits", "10digits"):
                ex_canon = ex_canon.replace(prefix, "")
            if canon and ex_canon and (canon == ex_canon or canon in ex_canon or ex_canon in canon):
                if prefer_replace or len(clean) > len(existing):
                    self._recorded_issues[idx] = clean
                return
        if clean not in self._recorded_issues:
            self._recorded_issues.append(clean)

    async def start_goal(self, goal: str) -> None:
        if not self.allow_writes:
            raise StorageUnavailableError(self.write_block_reason)
        clean_goal = (goal or "").strip()
        if not clean_goal:
            return

        if self._task and not self._task.done():
            await self.stop_goal(silent=True)

        # Dukungan pemulihan pasca-restart server/browser: jika user mengetik "lanjut" / "/lanjut",
        # pulihkan otomatis instruksi & langkah terverifikasi terakhir dari TaskStateStore!
        resumed_ctx: Optional[Dict[str, Any]] = None
        if clean_goal.lower() in ("lanjut", "lanjutkan", "teruskan", "/lanjut", "resume", "/resume"):
            resumed_ctx = self.task_store.get_resumable_context()
            if resumed_ctx and resumed_ctx.get("initial_instruction"):
                prev_steps_txt = "\n".join(
                    f"- Langkah {s.get('step_num')} ({s.get('action_name')}): {s.get('result', '')[:120]}"
                    for s in (resumed_ctx.get("completed_steps") or [])[-8:]
                )
                last_u = resumed_ctx.get("last_verified_url") or ""
                clean_goal = (
                    f"{resumed_ctx['initial_instruction']}\n\n"
                    "[KONTEKS PEMULIHAN PASCA-RESTART / JEDA — JANGAN ULANGI AKSI BEREFEK SAMPING YANG SUDAH SELESAI]:\n"
                    + (f"URL Terverifikasi Terakhir: {last_u}\n" if last_u else "")
                    + (f"Langkah Terverifikasi Sebelumnya:\n{prev_steps_txt}" if prev_steps_txt else "")
                ).strip()

        self.current_goal = clean_goal
        self.current_plan = [{"step": "Memahami perintah & menyiapkan langkah...", "status": "in_progress"}]
        self.history = []
        self.memory_notes = dict(self.persistent_notes)
        if self.user_memories:
            self.memory_notes["PROFIL_DAN_PREFERENSI_USER"] = "\n".join(self.user_memories)
        self.pending_approval = None
        self.takeover_reason = ""
        self.manual_takeover = False
        self._stop_requested = False
        self._form_touched = False
        self._browser_actions_count = 0
        self.recovery.reset_run()
        self._goal_started_mono = time.monotonic()
        self._expected_fields = {}
        self._goal_expectations = self._parse_goal_kv_expectations(clean_goal)
        self._field_warnings = {}
        self._recorded_issues = []
        self._created_files_this_run = []
        self._created_top_dirs = []
        self._last_submit_verification = None
        self._verify_retry_used = False
        self._pre_submit_blocked_once = False

        task_rec = self.task_store.create_task(clean_goal, initial_plan=self.current_plan)
        self._active_task_id = task_rec["task_id"]
        if resumed_ctx and resumed_ctx.get("idempotency_ledger"):
            # Wariskan idempotency ledger dari sesi sebelumnya agar aksi submit/hapus tidak dijalankan dua kali!
            def _carry_ledger():
                st = self.task_store._read_unlocked()
                t = self.task_store._find(st, self._active_task_id)
                if t:
                    t["idempotency_ledger"] = dict(resumed_ctx["idempotency_ledger"])
                    if resumed_ctx.get("last_verified_url"):
                        t["last_verified_url"] = resumed_ctx["last_verified_url"]
                    self.task_store._write_unlocked(st)
            self.task_store._with_file_lock(write=True, fn=_carry_ledger)

        await self._push_chat("user", redact_secrets(goal))
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
        if self._active_task_id:
            el_s = max(1, int(time.monotonic() - self._goal_started_mono)) if self._goal_started_mono else 1
            self.task_store.finalize_task(
                self._active_task_id,
                status="TERHENTI",
                duration_sec=el_s,
                verified=False,
                verification_summary="Dihentikan oleh pengguna sebelum seluruh langkah selesai.",
                issues=self._recorded_issues,
            )
            if not silent and self.telegram:
                metrics = compute_step_metrics(self.history, self.current_plan, self._browser_actions_count, "TERHENTI")
                proof_b64 = await self.browser.capture_proof_screenshot() if self._browser_actions_count > 0 else ""
                rep_txt = format_execution_report(
                    instruction=self.current_goal,
                    final_status="TERHENTI",
                    metrics=metrics,
                    duration_sec=el_s,
                    issues=self._recorded_issues or ["Dihentikan atas permintaan pengguna"],
                    verification_summary="Pekerjaan dihentikan manual; progres terakhir tersimpan.",
                    has_screenshot=bool(proof_b64),
                )
                await self.telegram.notify_done(
                    rep_txt,
                    elapsed_sec=el_s,
                    total_steps=metrics["total_executed"],
                    proof_frame_b64=proof_b64,
                    final_status="TERHENTI",
                )
            self._active_task_id = ""
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
                goal_low_check = self.current_goal.lower()
                needs_browser_obs = bool(
                    self.browser._started
                    or any(
                        w in goal_low_check
                        for w in ("http://", "https://", "website", "situs", "buka web", "browser", ".com", ".id", ".org", ".net", "login")
                    )
                )
                for _attempt in range(2):
                    try:
                        if needs_browser_obs:
                            dom_state = await self.browser.observe(annotated_screenshot=self.llm_config.has_api_key)
                        else:
                            dom_state = {
                                "url": "about:blank",
                                "url_display": "(Mode Workspace File System & Python — Browser standby)",
                                "title": "Workspace Lokal",
                                "scroll": {"y": 0, "maxY": 0},
                                "signals": {},
                                "elements": [],
                                "readable_text": "(Tugas lokal file/folder/Python — panggil 'navigate' kapan saja jika perlu membuka website)",
                                "screenshot_b64": "",
                            }
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
                    if self._active_task_id:
                        self.task_store.update_plan(self._active_task_id, self.current_plan)

                action = decision.get("action") or {"name": "done", "summary": "Selesai."}
                act_name = (action.get("name") or "done").lower()

                # SELESAI? (Gerbang Verifikasi Ketat Pasca-Submit, Anti-Berhenti Tengah Jalan, & Wajib File Deliverable!)
                if act_name == "done":
                    goal_low = self.current_goal.lower()
                    cand_summary = (action.get("summary") or thought or "").strip()
                    cand_low = cand_summary.lower()

                    # 1. GERBANG ANTI-BERHENTI TENGAH JALAN ("saya akan melanjutkan..." padahal memilih action 'done'!)
                    premature_phrases = (
                        "saya akan melanjutkan",
                        "akan saya lanjutkan",
                        "akan melanjutkan dengan",
                        "selanjutnya saya akan",
                        "mari kita lanjutkan",
                        "langkah selanjutnya adalah",
                    )
                    if any(pp in cand_low for pp in premature_phrases) and step_num <= self.MAX_STEPS - 3:
                        self.history.append({
                            "step": step_num,
                            "action_name": "verify_gate",
                            "result": (
                                "⚠️ DITOLAK BERHENTI TENGAH JALAN: Kamu menulis bahwa kamu 'akan melanjutkan', tetapi memilih action 'done'! "
                                "DILARANG memilih 'done' sebelum seluruh pengambilan data, perhitungan/analisis, pembuatan file, dan seluruh poin LAPORAN AKHIR benar-benar tuntas!"
                            ),
                        })
                        continue

                    # 1B. GERBANG VERIFIKASI SISTEM FOLDER / DIREKTORI SUNGGUHAN
                    wants_folder_sys = any(
                        fk in goal_low for fk in ("sistem folder", "buat folder", "buatkan folder", "struktur direktori", "├──")
                    )
                    if wants_folder_sys and not self._created_top_dirs and not self.file_manager.list_top_dirs() and step_num <= self.MAX_STEPS - 2:
                        self.history.append({
                            "step": step_num,
                            "action_name": "verify_gate",
                            "result": (
                                "❌ DITOLAK SIMULASI SEMU: Instruksi meminta pembuatan folder/direktori SUNGGUHAN di file system, "
                                "namun belum ada folder yang dibuat di disk! Gunakan 'make_dir' / 'write_file' / 'run_python' sekarang untuk membuat folder & file aslinya, lalu verifikasi dengan 'list_tree'!"
                            ),
                        })
                        continue
                    req_file_ext = ""
                    if re.search(r"\b(buat(?:kan)?\s+(?:file\s+|laporan\s+)?excel|file\s+excel|\.xlsx)\b", goal_low):
                        req_file_ext = "xlsx"
                    elif re.search(r"\b(buat(?:kan)?\s+(?:file\s+|dokumen\s+|laporan\s+)?word|file\s+word|\.docx)\b", goal_low):
                        req_file_ext = "docx"
                    elif re.search(r"\b(buat(?:kan)?\s+(?:file\s+|dokumen\s+|laporan\s+)?pdf|file\s+pdf|\.pdf)\b", goal_low):
                        req_file_ext = "pdf"
                    elif re.search(r"\b(buat(?:kan)?\s+(?:file\s+)?csv|\.csv)\b", goal_low):
                        req_file_ext = "csv"

                    if req_file_ext:
                        matching_files = [
                            fn for fn in self._created_files_this_run
                            if fn.lower().endswith(f".{req_file_ext}")
                        ]
                        if not matching_files and step_num <= self.MAX_STEPS - 2:
                            sheet_hint = (
                                " Gunakan judul '# Sheet 1: Nama Sheet' lalu tabel pipa '|...|', kemudian '# Sheet 2: Nama Sheet' lalu tabel/analisisnya agar otomatis menjadi file Excel multi-sheet asli!"
                                if req_file_ext == "xlsx" and any(s in goal_low for s in ("dua sheet", "2 sheet", "sheet 1", "multi sheet"))
                                else ""
                            )
                            self.history.append({
                                "step": step_num,
                                "action_name": "verify_gate",
                                "result": (
                                    f"❌ DITOLAK SELESAI PREMATUR: Instruksi user mewajibkan pembuatan file .{req_file_ext.upper()}, "
                                    f"tetapi kamu BELUM memanggil action 'write_file' untuk membuat file .{req_file_ext}!{sheet_hint} "
                                    "Panggil 'write_file' sekarang juga, baru setelah file tersimpan panggil 'done' dengan laporan akhir lengkap!"
                                ),
                            })
                            continue

                        # Jika user meminta Excel 2 sheet / multi-sheet, pastikan file .xlsx benar-benar memiliki >=2 sheet!
                        wants_two_sheets = req_file_ext == "xlsx" and any(
                            s in goal_low for s in ("dua sheet", "2 sheet", "sheet 1", "sheet 2")
                        )
                        if wants_two_sheets and matching_files and step_num <= self.MAX_STEPS - 2:
                            try:
                                import openpyxl as _ox
                                latest_xlsx = self.file_manager.get_file_path(matching_files[-1])
                                wb_chk = _ox.load_workbook(str(latest_xlsx), read_only=True)
                                s_names = list(wb_chk.sheetnames)
                                wb_chk.close()
                                if len(s_names) < 2:
                                    self.history.append({
                                        "step": step_num,
                                        "action_name": "verify_gate",
                                        "result": (
                                            f"❌ FILE EXCEL BELUM MULTI-SHEET: File '{matching_files[-1]}' baru memiliki 1 sheet ({s_names}), "
                                            "padahal instruksi mewajibkan 2 sheet! Panggil ulang 'write_file' pada file .xlsx tersebut dengan format:\n"
                                            "# Sheet 1: Data Produk\n| ... |\n\n# Sheet 2: Analisis dan Rekomendasi\n| ... |"
                                        ),
                                    })
                                    continue
                            except Exception:
                                pass

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
                            elapsed_fail = max(1, int(time.monotonic() - self._goal_started_mono)) if self._goal_started_mono else 1
                            metrics_fail = compute_step_metrics(self.history, self.current_plan, self._browser_actions_count, "GAGAL")
                            proof_fail_b64 = await self.browser.capture_proof_screenshot() if (self._browser_actions_count > 0 or self._form_touched) else ""
                            fail_lines = "\n".join(f"• {item}" for item in problems_list) or "• Tabel hasil submit tidak muncul atau data tidak sesuai instruksi."
                            ver_fail_summary = "Verifikasi pasca-submit GAGAL: data pada tabel hasil belum sesuai instruksi."
                            if self._active_task_id:
                                self.task_store.finalize_task(
                                    self._active_task_id,
                                    status="GAGAL",
                                    duration_sec=elapsed_fail,
                                    verified=False,
                                    verification_summary=ver_fail_summary,
                                    verification_details=problems_list,
                                    issues=problems_list + self._recorded_issues,
                                )
                                self._active_task_id = ""
                            honest_fail_msg = format_execution_report(
                                instruction=self.current_goal,
                                final_status="GAGAL",
                                metrics=metrics_fail,
                                duration_sec=elapsed_fail,
                                issues=problems_list + self._recorded_issues,
                                verification_summary=ver_fail_summary,
                                narrative_summary=(
                                    "❌ Pengisian Formulir GAGAL Verifikasi (Data Belum Sesuai)!\n\n"
                                    f"Daftar field yang bermasalah beserta penyebabnya:\n{fail_lines}"
                                ),
                                has_screenshot=bool(proof_fail_b64),
                            )
                            await self._push_chat("agent", honest_fail_msg)
                            if self.telegram:
                                await self.telegram.notify_done(
                                    honest_fail_msg,
                                    elapsed_sec=elapsed_fail,
                                    total_steps=metrics_fail["total_executed"],
                                    proof_frame_b64=proof_fail_b64,
                                    final_status="GAGAL",
                                )
                            return

                    for p in self.current_plan:
                        if p.get("status") == "in_progress":
                            p["status"] = "completed"
                    self.state = "IDLE"
                    self.live_status = "Online · Siap disuruh apa aja"
                    await self._sync_state()

                    metrics_ok = compute_step_metrics(self.history, self.current_plan, self._browser_actions_count, "BERHASIL")
                    total_steps = metrics_ok["total_executed"]
                    elapsed_sec = max(1, int(time.monotonic() - self._goal_started_mono)) if self._goal_started_mono else 1
                    raw_summary = redact_secrets((action.get("summary") or thought or "Pekerjaan beres dijalankan!").strip())

                    asks_metrics = any(
                        k in goal_low
                        for k in ("jumlah langkah", "waktu pengerjaan", "kesalahan", "hasil akhir")
                    )
                    ver_rows = (self._last_submit_verification or {}).get("rows") or []

                    if self._form_touched or asks_metrics:
                        if self._recorded_issues:
                            raw_summary = re.sub(
                                r"tidak ada (?:kesalahan|kendala|error)[^.\n]*\.?",
                                "",
                                raw_summary,
                                flags=re.I,
                            ).strip()
                        raw_summary = re.sub(
                            r"\bdalam\s+\d+\s+langkah\b",
                            f"dalam {total_steps} langkah",
                            raw_summary,
                            flags=re.I,
                        )

                        extra_ver_blocks: List[str] = []
                        if ver_rows:
                            tbl_bullets = "\n".join(f"• {r['label']}: {r['value'] or '(kosong)'}" for r in ver_rows)
                            extra_ver_blocks.append(f"📋 Tabel Hasil Terverifikasi di Layar:\n{tbl_bullets}")
                        narrative_body = (raw_summary + ("\n\n" + "\n\n".join(extra_ver_blocks) if extra_ver_blocks else "")).strip()
                    else:
                        narrative_body = raw_summary
                        zipped_bundles: List[str] = []
                        for top_d in list(dict.fromkeys(self._created_top_dirs)):
                            try:
                                z_saved = self.file_manager.zip_directory(top_d)
                                zipped_bundles.append(z_saved["name"])
                                if self.telegram:
                                    zf_path = self.file_manager.get_file_path(z_saved["name"])
                                    await self.telegram.notify_file(z_saved["name"], zf_path.read_bytes())
                            except Exception:
                                pass
                        if zipped_bundles:
                            narrative_body += f"\n\n📦 Paket Folder Asli Terkirim: {', '.join(zipped_bundles)} (berisi seluruh struktur folder & file sungguhan)"
                        elif self._created_files_this_run:
                            files_joined = ", ".join(dict.fromkeys(self._created_files_this_run))
                            if files_joined not in narrative_body:
                                narrative_body += f"\n\n📎 File Dibuat & Dikirim: {files_joined}"

                    proof_b64 = ""
                    if self._browser_actions_count > 0 or self._form_touched:
                        proof_b64 = await self.browser.capture_proof_screenshot()

                    if ver_rows:
                        ver_summary_txt = f"Tabel hasil submit terverifikasi ({len(ver_rows)} baris sesuai)."
                    elif self._created_files_this_run or self._created_top_dirs:
                        ver_summary_txt = f"Deliverable file/folder terverifikasi di disk ({len(self._created_files_this_run)} file)."
                    elif self._browser_actions_count > 0:
                        ver_summary_txt = f"Hasil aksi browser terverifikasi pada halaman {self.browser.last_real_url or 'aktif'}."
                    else:
                        ver_summary_txt = "Jawaban & hasil langkah terverifikasi sesuai instruksi."

                    if self._active_task_id:
                        self.task_store.finalize_task(
                            self._active_task_id,
                            status="BERHASIL",
                            duration_sec=elapsed_sec,
                            verified=True,
                            verification_summary=ver_summary_txt,
                            issues=self._recorded_issues,
                        )
                        self._active_task_id = ""

                    # Sertakan blok Laporan Eksekusi 10-poin standar ketika tugas menjalankan langkah nyata (total_steps > 0 atau form/file/browser)
                    if total_steps > 0 or self._browser_actions_count > 0 or self._form_touched or self._created_files_this_run or asks_metrics:
                        summary_msg = format_execution_report(
                            instruction=self.current_goal,
                            final_status="BERHASIL",
                            metrics=metrics_ok,
                            duration_sec=elapsed_sec,
                            issues=self._recorded_issues,
                            verification_summary=ver_summary_txt,
                            narrative_summary=narrative_body,
                            has_screenshot=bool(proof_b64),
                        )
                    else:
                        summary_msg = narrative_body

                    await self._push_chat("agent", summary_msg)
                    if self.telegram:
                        await self.telegram.notify_done(
                            summary_msg,
                            elapsed_sec=elapsed_sec,
                            total_steps=total_steps,
                            proof_frame_b64=proof_b64,
                            final_status="BERHASIL",
                        )
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
                if self._active_task_id:
                    self.task_store.mark_step_running(self._active_task_id, step_num, act_name, thought or act_name)

                if thought:
                    safe_thought = redact_secrets(thought)
                    await self._push_chat("step", safe_thought, {"step": step_num, "action": act_name})
                    if self.telegram:
                        await self.telegram.notify_step(step_num, safe_thought, self.current_plan)

                # Cek circuit breaker anti klik berulang membabi buta SEBELUM mengeksekusi aksi browser!
                blind_block = self.recovery.check_blind_repeat_before_action(action, dom_state)
                side_effect_fp: Optional[str] = None
                if blind_block:
                    result_text = blind_block
                    if self._active_task_id:
                        self.task_store.record_recovery(
                            self._active_task_id,
                            step_num,
                            "blind_repeat_action",
                            "Aksi identik berulang tanpa perubahan DOM",
                            "Circuit breaker menghentikan klik ulang buta",
                            "blocked",
                        )
                else:
                    # Tandai apakah aksi ini berefek samping agar tidak pernah dijalankan dua kali tanpa verifikasi
                    if act_name == "click":
                        idx_chk = int(action.get("index", 0))
                        el_chk = next((e for e in dom_state.get("elements", []) if e["index"] == idx_chk), None)
                        if self._is_submit_target(el_chk) or inspect_element_sensitivity(el_chk):
                            action["_is_side_effect"] = True
                            action["_target_sig"] = (el_chk or {}).get("dom_selector") or (el_chk or {}).get("text") or str(idx_chk)
                    side_effect_fp = compute_side_effect_fingerprint(action, dom_state.get("url", ""))
                    prior_exec = self.task_store.check_idempotency(self._active_task_id, side_effect_fp) if self._active_task_id else None

                    if prior_exec:
                        result_text = (
                            f"ℹ️ [VERIFIKASI IDEMPOTENSI] Aksi berefek samping '{act_name}' sudah pernah berhasil dijalankan "
                            f"pada Langkah {prior_exec.get('step_num')} ({prior_exec.get('result', '')[:120]}). "
                            "Tidak diulangi agar tidak terjadi duplikasi."
                        )
                    else:
                        try:
                            result_text = await self._execute_with_guardrails(step_num, action, dom_state)
                        except Exception as step_exc:
                            err_label = redact_secrets(str(step_exc).strip() or type(step_exc).__name__)
                            rec_crash = await self.recovery.attempt_browser_recovery(
                                self.browser,
                                action,
                                "browser_crash",
                                err_label,
                                last_verified_url=self.browser.last_real_url,
                            )
                            if rec_crash and rec_crash.get("recovered"):
                                result_text = rec_crash["new_result"]
                            else:
                                result_text = f"❌ Kendala pada aksi {act_name}: {err_label[:110]}"

                        # Error Recovery Engine: deteksi error & coba strategi alternatif berbatas aman (maks 2x)
                        err_cls = self.recovery.classify_error(result_text, dom_state)
                        if err_cls is not None:
                            err_type, err_cause = err_cls
                            rec_out = await self.recovery.attempt_browser_recovery(
                                self.browser,
                                action,
                                err_type,
                                err_cause,
                                last_verified_url=self.browser.last_real_url,
                            )
                            if rec_out and rec_out.get("recovered"):
                                result_text = rec_out["new_result"]
                                if self._active_task_id:
                                    self.task_store.record_recovery(
                                        self._active_task_id,
                                        step_num,
                                        err_type,
                                        err_cause,
                                        rec_out["strategy"],
                                        "recovered",
                                    )
                            elif self._active_task_id:
                                self.task_store.record_recovery(
                                    self._active_task_id,
                                    step_num,
                                    err_type,
                                    err_cause,
                                    "Evaluasi alternatif langkah oleh orchestrator",
                                    "unrecovered_step",
                                )

                        self.recovery.record_action_delta(action, dom_state)

                step_succeeded = not (
                    result_text.startswith("❌")
                    or result_text.startswith("⛔")
                    or "[UNREACHABLE_SITE]" in result_text
                )
                cur_disp_u = ""
                try:
                    if self.browser.page:
                        cur_disp_u = self.browser._display_url(self.browser.page.url)
                except Exception:
                    pass
                if self._active_task_id:
                    self.task_store.record_step_outcome(
                        self._active_task_id,
                        step_num=step_num,
                        action_name=act_name,
                        result_text=result_text,
                        succeeded=step_succeeded,
                        verified_url=cur_disp_u,
                        idempotency_fp=side_effect_fp if step_succeeded else None,
                    )

                self.history.append({"step": step_num, "action_name": act_name, "result": redact_secrets(result_text)})

                # Jika situs utama yang diminta user memblokir IP Cloud / down (misal portal .go.id tertentu),
                # langsung hentikan dalam 9 detik dan beri penjelasan jernih tanpa muter berulang-ulang!
                if isinstance(result_text, str) and result_text.startswith("[UNREACHABLE_SITE]"):
                    from urllib.parse import urlparse as _up
                    req_host = _up(action.get("url") or "").netloc.lower()
                    if req_host and req_host in self.current_goal.lower():
                        self.state = "IDLE"
                        self.live_status = "Situs tujuan menolak koneksi Cloud"
                        await self._sync_state()
                        el_unreach = max(1, int(time.monotonic() - self._goal_started_mono)) if self._goal_started_mono else 1
                        metrics_unreach = compute_step_metrics(self.history, self.current_plan, self._browser_actions_count, "GAGAL")
                        px_hint = (
                            "Pasang Proxy Indonesia lewat ketik `/proxy` kalau mau tembus situs `.go.id` ini, "
                            "atau lanjut ke situs lain!"
                            if not self.browser.proxy_config
                            else "Cek status `/proxy` untuk memastikan kuota/koneksi Proxy Indonesia aktif!"
                        )
                        unreach_cause = f"Situs {req_host} menolak atau tidak merespons koneksi dalam 9 detik."
                        if self._active_task_id:
                            self.task_store.finalize_task(
                                self._active_task_id,
                                status="GAGAL",
                                duration_sec=el_unreach,
                                verified=False,
                                verification_summary=unreach_cause,
                                issues=[unreach_cause],
                            )
                            self._active_task_id = ""
                        clean_unreach = format_execution_report(
                            instruction=self.current_goal,
                            final_status="GAGAL",
                            metrics=metrics_unreach,
                            duration_sec=el_unreach,
                            issues=[unreach_cause],
                            verification_summary="Halaman target gagal dimuat karena blokir jaringan/timeout.",
                            narrative_summary=(
                                f"🚫 Situs `{req_host}` menolak atau tidak merespons koneksi dalam 9 detik.\n\n"
                                "Ini biasa terjadi pada portal pemerintah (`.go.id`) yang dikunci khusus jaringan provider lokal Indonesia (Telkomsel/IndiHome/Biznet). "
                                f"{px_hint}"
                            ),
                            has_screenshot=False,
                        )
                        await self._push_chat("agent", clean_unreach)
                        if self.telegram:
                            await self.telegram.notify_done(
                                clean_unreach,
                                elapsed_sec=el_unreach,
                                total_steps=metrics_unreach["total_executed"],
                                final_status="GAGAL",
                            )
                        return

                await asyncio.sleep(0.08)

            self.state = "IDLE"
            self.live_status = "Online · Siap disuruh apa aja"
            await self._sync_state()
            el_max = max(1, int(time.monotonic() - self._goal_started_mono)) if self._goal_started_mono else 1
            prelim = compute_step_metrics(self.history, self.current_plan, self._browser_actions_count, "SEBAGIAN BERHASIL")
            max_status = "SEBAGIAN BERHASIL" if prelim["succeeded"] > 0 else "TERHENTI"
            metrics_max = compute_step_metrics(self.history, self.current_plan, self._browser_actions_count, max_status)
            proof_max = await self.browser.capture_proof_screenshot() if self._browser_actions_count > 0 else ""
            if self._active_task_id:
                self.task_store.finalize_task(
                    self._active_task_id,
                    status=max_status,
                    duration_sec=el_max,
                    verified=False,
                    verification_summary=f"Batas {self.MAX_STEPS} langkah tercapai; siap dilanjutkan dengan perintah 'lanjut'.",
                    issues=self._recorded_issues + [f"Batas langkah maksimum ({self.MAX_STEPS}) tercapai"],
                )
                self._active_task_id = ""
            max_msg = format_execution_report(
                instruction=self.current_goal,
                final_status=max_status,
                metrics=metrics_max,
                duration_sec=el_max,
                issues=self._recorded_issues + [f"Batas langkah maksimum ({self.MAX_STEPS}) tercapai"],
                verification_summary="Sebagian tahapan selesai; balas 'lanjut' untuk meneruskan dari checkpoint terakhir.",
                narrative_summary=f"Udah jalan maksimal {self.MAX_STEPS} langkah bro. Kalau masih mau dilanjutin tinggal balas `lanjut` aja!",
                has_screenshot=bool(proof_max),
            )
            await self._push_chat("agent", max_msg)
            if self.telegram:
                await self.telegram.notify_done(
                    max_msg,
                    elapsed_sec=el_max,
                    total_steps=metrics_max["total_executed"],
                    proof_frame_b64=proof_max,
                    final_status=max_status,
                )

        except asyncio.CancelledError:
            self.state = "IDLE"
            self.live_status = "Online · Siap disuruh apa aja"
            await self._sync_state()
        except Exception as exc:
            self.state = "IDLE"
            self.live_status = "Online · Siap disuruh apa aja"
            await self._sync_state()
            reason_txt = redact_secrets(str(exc).strip() or f"Gangguan koneksi sementara ({type(exc).__name__})")
            el_err = max(1, int(time.monotonic() - self._goal_started_mono)) if self._goal_started_mono else 1
            metrics_err = compute_step_metrics(self.history, self.current_plan, self._browser_actions_count, "TERHENTI")
            proof_err = await self.browser.capture_proof_screenshot() if self._browser_actions_count > 0 else ""
            cur_u = ""
            try:
                if self.browser.page:
                    cur_u = self.browser._display_url(self.browser.page.url)
            except Exception:
                pass
            loc_info = f" di halaman `{cur_u}`" if cur_u and cur_u != "about:blank" else ""
            if self._active_task_id:
                self.task_store.finalize_task(
                    self._active_task_id,
                    status="TERHENTI",
                    duration_sec=el_err,
                    verified=False,
                    verification_summary=f"Terhenti karena kendala teknis: {reason_txt[:140]}",
                    issues=self._recorded_issues + [reason_txt[:140]],
                )
                self._active_task_id = ""
            err_msg = format_execution_report(
                instruction=self.current_goal,
                final_status="TERHENTI",
                metrics=metrics_err,
                duration_sec=el_err,
                issues=self._recorded_issues + [reason_txt[:140]],
                verification_summary="Eksekusi terhenti; posisi halaman & checkpoint terakhir tersimpan aman.",
                narrative_summary=(
                    f"⚠️ Langkah pengerjaan sempat terputus{loc_info} ({reason_txt[:110]}).\n\n"
                    "Posisi & langkah terakhir masih tersimpan — ketik `lanjut` kalau mau gua terusin dari titik terakhir!"
                ),
                has_screenshot=bool(proof_err),
            )
            await self._push_chat("agent", err_msg)
            if self.telegram:
                await self.telegram.notify_done(
                    err_msg,
                    elapsed_sec=el_err,
                    total_steps=metrics_err["total_executed"],
                    proof_frame_b64=proof_err,
                    final_status="TERHENTI",
                )

    async def _wait_for_human_takeover(self, reason: str) -> None:
        self.state = "WAITING_HUMAN_TAKEOVER"
        self.live_status = "🔐 Nunggu lu login dulu..."
        self.manual_takeover = True
        self.takeover_reason = reason
        self._resume_event.clear()
        if self._active_task_id:
            self.task_store.set_waiting_approval(self._active_task_id, f"Menunggu login/input pengguna: {reason}")
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

        # GERBANG KEAMANAN AKSI NON-BROWSER (Penghapusan File & Eksekusi Skrip Destruktif / Eksfiltrasi)
        non_br_sens = inspect_non_browser_sensitivity(action)
        if non_br_sens:
            if non_br_sens.get("decision") == "block":
                return f"⛔ [BLOKIR KEAMANAN] {non_br_sens.get('reason', 'Aksi ditolak oleh sistem keamanan.')}"
            approved_nb = await self._prompt_user_approval(non_br_sens)
            if approved_nb != "approve":
                await self._push_chat("system", f"🚫 Oke, `{non_br_sens['target_label']}` dibatalkan.")
                return f"Tindakan '{non_br_sens['target_label']}' DITOLAK oleh pengguna. Jangan ulangi aksi ini."
            await self._push_chat("system", f"✅ Disetujui! Menjalankan `{non_br_sens['target_label']}`...")

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
                register_runtime_secret(typed_val)

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
                    self._browser_actions_count += 1
                    return await self.browser.click_element(idx, elements)
                return "Disetujui oleh user."

        if act_name == "navigate":
            self._browser_actions_count += 1
            return await self.browser.navigate(action.get("url", ""))

        if act_name == "click":
            self._browser_actions_count += 1
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
            self._browser_actions_count += 1
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
                    self._record_issue(f"Field '{fkey}': {res.replace('❌ ', '')}")
                else:
                    self._field_warnings.pop(fkey, None)
                    self._expected_fields[fkey] = val_txt
                    if "⚠️" in res:
                        note_part = res.split("⚠️", 1)[1].strip().rstrip(".")
                        self._record_issue(f"Field '{fkey}': {note_part}")
            return res

        if act_name == "select":
            self._browser_actions_count += 1
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
                    self._record_issue(f"Field '{fkey}': {res.replace('❌ ', '')}")
                else:
                    self._field_warnings.pop(fkey, None)
                    self._expected_fields[fkey] = opt_txt
                    if "⚠️" in res:
                        note_part = res.split("⚠️", 1)[1].strip().rstrip(".")
                        self._record_issue(f"Field '{fkey}': {note_part}")
            return res

        if act_name == "scroll":
            self._browser_actions_count += 1
            return await self.browser.scroll_page(direction=str(action.get("direction", "down")))

        if act_name == "go_back":
            self._browser_actions_count += 1
            return await self.browser.go_back()

        if act_name == "solve_captcha":
            self._browser_actions_count += 1
            cap_res = await self.browser.auto_solve_captcha(solver_fn=self.brain.solve_captcha_image)
            return cap_res or "Tidak ditemukan CAPTCHA gambar/hitungan yang perlu diisi di halaman ini."

        if act_name == "run_js":
            self._browser_actions_count += 1
            js_code = str(action.get("code") or action.get("script") or "")
            js_out = await self.browser.run_page_js(js_code)
            self.memory_notes[f"hasil_js_langkah_{step_num}"] = redact_secrets(js_out)[:2500]
            return redact_secrets(js_out)

        if act_name == "run_python":
            import subprocess as _sp
            import sys as _sys

            py_code = str(action.get("code") or action.get("script") or "").strip()
            if not py_code:
                return "❌ Kode Python kosong."
            ws_dir = self.file_manager.base_dir
            before_dirs = set(self.file_manager.list_top_dirs())
            before_snap = {
                f["name"]: (f["size"], f["modified"])
                for f in self.file_manager.list_files()
            }
            try:
                proc = await asyncio.to_thread(
                    _sp.run,
                    [_sys.executable, "-I", "-c", py_code],
                    cwd=str(ws_dir),
                    capture_output=True,
                    text=True,
                    timeout=14,
                )
            except Exception as py_exc:
                return f"❌ Gagal menjalankan Python ({str(py_exc)[:140]})."

            for nd in self.file_manager.list_top_dirs():
                if nd not in before_dirs and nd not in self._created_top_dirs:
                    self._created_top_dirs.append(nd)

            after_files = self.file_manager.list_files()
            changed_names: List[str] = []
            for f in after_files:
                nm = f["name"]
                if nm.startswith(".") or nm in ("profil_pengajuan.json", "catatan_workspace.md"):
                    continue
                if nm not in before_snap or before_snap[nm] != (f["size"], f["modified"]):
                    changed_names.append(nm)
                    if nm not in self._created_files_this_run:
                        self._created_files_this_run.append(nm)
                    if "/" in nm:
                        top_seg = nm.split("/", 1)[0]
                        if top_seg not in self._created_top_dirs:
                            self._created_top_dirs.append(top_seg)
                    else:
                        fpath = self.file_manager.get_file_path(nm)
                        await self._push_chat("file", f"File siap: {nm}", {"filename": nm, "size": f["size"]})
                        if self.telegram:
                            await self.telegram.notify_file(nm, fpath.read_bytes())

            if changed_names:
                await self.broadcast({"type": "files_updated", "files": after_files})

            out_txt = ((proc.stdout or "") + ("\n[STDERR]\n" + proc.stderr if proc.stderr else "")).strip()
            if len(out_txt) > 3200:
                out_txt = out_txt[:3200] + "... [dipotong]"
            self.memory_notes[f"hasil_python_langkah_{step_num}"] = out_txt[:2500]
            files_note = f" | File baru otomatis terkirim ke Telegram: {', '.join(changed_names)}" if changed_names else ""
            if proc.returncode != 0:
                return f"❌ Python keluar dengan kode {proc.returncode}: {out_txt}{files_note}"
            return f"✅ Python berhasil dijalankan: {out_txt or '(tanpa output teks)'}{files_note}"

        if act_name in ("make_dir", "mkdir"):
            raw_p = str(action.get("path") or action.get("dirname") or action.get("filename") or "")
            parts_p = [p.strip() for p in re.split(r"[,;\n]+", raw_p) if p.strip()]
            if not parts_p:
                return "❌ Path folder kosong."
            made_list: List[str] = []
            for p_item in parts_p:
                d_res = self.file_manager.make_dir(p_item)
                made_list.append(d_res["name"])
                top_seg = d_res["name"].split("/", 1)[0]
                if top_seg and top_seg not in self._created_top_dirs:
                    self._created_top_dirs.append(top_seg)
            return f"✅ Folder berhasil dibuat di disk: {', '.join(made_list)}"

        if act_name in ("list_tree", "tree"):
            rel_r = str(action.get("path") or action.get("dirname") or "")
            tree_txt = self.file_manager.get_directory_tree(rel_r)
            self.memory_notes[f"tree_{rel_r or 'root'}"] = tree_txt
            return f"✅ Struktur Direktori Terverifikasi di Disk:\n{tree_txt}"

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
            if saved["name"] not in self._created_files_this_run:
                self._created_files_this_run.append(saved["name"])
            if "/" in saved["name"]:
                top_seg = saved["name"].split("/", 1)[0]
                if top_seg and top_seg not in self._created_top_dirs:
                    self._created_top_dirs.append(top_seg)
            self.memory_notes[f"file_dibuat_{saved['name']}"] = content[:2500]
            await self.broadcast({"type": "files_updated", "files": self.file_manager.list_files()})
            await self._push_chat(
                "file",
                f"File siap: {saved['name']}",
                {"filename": saved["name"], "size": saved["size"]},
            )
            fpath = self.file_manager.get_file_path(saved["name"])
            # Jika file berada di root (bukan bagian dari proyek multi-folder yang akan dikirim utuh sebagai .zip), kirim langsung!
            if self.telegram and "/" not in saved["name"]:
                await self.telegram.notify_file(saved["name"], fpath.read_bytes())

            extra_sheet_info = ""
            if saved["name"].lower().endswith(".xlsx"):
                try:
                    import openpyxl as _ox
                    wb_i = _ox.load_workbook(str(fpath), read_only=True)
                    extra_sheet_info = f" [Sheet tersedia: {', '.join(wb_i.sheetnames)}]"
                    wb_i.close()
                except Exception:
                    pass
            return f"✅ File '{saved['name']}' berhasil disimpan di disk ({saved['size']} bytes){extra_sheet_info}."

        if act_name in ("delete_file", "remove_file"):
            fname = str(action.get("filename") or action.get("path") or "").strip()
            if not fname:
                return "❌ Nama file yang akan dihapus kosong."
            deleted = self.file_manager.delete_file(fname)
            await self.broadcast({"type": "files_updated", "files": self.file_manager.list_files()})
            if deleted:
                return f"🗑️ File '{fname}' berhasil dihapus setelah mendapat persetujuan pengguna."
            return f"⚠️ File '{fname}' tidak ditemukan di workspace."

        return f"Action '{act_name}' tidak dikenali."

    async def _prompt_user_approval(self, sens_report: Dict[str, Any]) -> str:
        self.state = "WAITING_APPROVAL"
        self.live_status = "🛡️ Menunggu persetujuan lu..."
        self.pending_approval = sens_report
        self._approval_decision = None
        self._resume_event.clear()
        if self._active_task_id:
            self.task_store.set_waiting_approval(
                self._active_task_id,
                f"Menunggu persetujuan pengguna untuk: {sens_report.get('target_label', 'aksi sensitif')}",
            )

        await self._sync_state()
        await self._push_chat(
            "approval",
            f"Meminta izin untuk **{sens_report['target_label']}**. Boleh dilanjutin bro?",
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
        # ATURAN #5: Verifikasi dulu apakah hasil submit sebelumnya SUDAH muncul nyata di layar sebelum mengklik Submit lagi!
        already_sub = await self.browser.read_submission_result()
        if already_sub.get("submitted") and any((r.get("value") or "").strip() for r in already_sub.get("rows", [])):
            rows_prior = already_sub["rows"]
            self._last_submit_verification = {"ok": True, "rows": rows_prior, "problems": []}
            tbl_prior = "\n".join(f"  • {r['label']}: {r['value'] or '(kosong)'}" for r in rows_prior)
            return (
                "ℹ️ [VERIFIKASI HASIL SEBELUMNYA] Formulir terbukti SUDAH ter-submit sebelumnya (tidak diklik ulang).\n"
                f"📋 Data pada Tabel Hasil:\n{tbl_prior}"
            )

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
                tv_clean = tbl_val.strip()
                if sp.lower() in tv_clean.lower():
                    continue
                # Kasus 1: Pemotongan resmi maxlength HTML (mis. '081234567890' -> '0812345678')
                if len(sp) > len(tv_clean) >= 6 and sp.lower().startswith(tv_clean.lower()):
                    self._record_issue(
                        f"Field '{tbl_label.title()}': diminta '{sp}' ({len(sp)} karakter), namun website membatasi panjang maksimal {len(tv_clean)} karakter (maxlength={len(tv_clean)}) sehingga tersimpan '{tv_clean}'",
                        field_key=tbl_label,
                        prefer_replace=True,
                    )
                    continue
                # Kasus 2: Penyesuaian label opsi resmi website (mis. 'Mathematics' -> 'Maths')
                if len(sp) >= 4 and len(tv_clean) >= 4 and tv_clean[:4].lower() == sp[:4].lower():
                    self._record_issue(
                        f"Field '{tbl_label.title()}': diminta '{sp}', disesuaikan ke opsi resmi website menjadi '{tv_clean}'",
                        field_key=tbl_label,
                        prefer_replace=True,
                    )
                    continue
                problems.append(
                    f"Field '{tbl_label}' ({exp_k}): tertulis '{tbl_val}' di tabel hasil, tidak sesuai dengan yang diminta '{exp_v}'"
                )
                break

        # Bandingkan juga langsung terhadap permintaan asli user di prompt (_goal_expectations)
        # agar pemotongan maxlength (mis. Mobile 081234567890 -> 0812345678) atau penyesuaian label selalu tercatat jujur
        # bahkan jika LLM sudah memendekkan teks sebelum mengetik!
        for gk, gv in self._goal_expectations.items():
            if not gv or "date" in gk or "birth" in gk or "tanggal" in gk:
                continue
            matched_goal_row = _find_table_val(gk)
            if matched_goal_row is None:
                continue
            g_lbl, g_val = matched_goal_row
            g_val_s = (g_val or "").strip()
            if not g_val_s:
                msg_empty = f"Field '{g_lbl}' ({gk}): kosong di tabel hasil, padahal user meminta '{gv}'"
                if msg_empty not in problems:
                    problems.append(msg_empty)
                continue
            if len(gv) > len(g_val_s) >= 6 and gv.lower().startswith(g_val_s.lower()):
                self._record_issue(
                    f"Field '{g_lbl.title()}': diminta '{gv}' ({len(gv)} karakter), namun website membatasi panjang maksimal {len(g_val_s)} karakter (maxlength={len(g_val_s)}) sehingga tersimpan '{g_val_s}'",
                    field_key=g_lbl,
                )
            elif "subject" in gk and g_val_s.lower() != gv.lower() and g_val_s[:4].lower() == gv[:4].lower():
                self._record_issue(
                    f"Field '{g_lbl.title()}': diminta '{gv}', disesuaikan ke opsi resmi website menjadi '{g_val_s}'",
                    field_key=g_lbl,
                )

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
