from datetime import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.security import StorageUnavailableError, redact_secrets, redact_structure

VALID_FINAL_STATUSES = (
    "BERHASIL",
    "GAGAL",
    "SEBAGIAN BERHASIL",
    "MENUNGGU PERSETUJUAN",
    "TERHENTI",
)


def summarize_task_name(instruction: str) -> str:
    """Merangkum instruksi awal menjadi nama pekerjaan singkat & jelas (maks 68 karakter)."""
    clean = redact_secrets(instruction or "").strip()
    if not clean:
        return "Tugas Otomatisasi"
    first_meaningful = ""
    for ln in clean.splitlines():
        s = re.sub(r"^[\s#*\-•\d\.\)]+", "", ln).strip()
        if not s:
            continue
        if s.upper() in ("TUGAS", "TUGAS:", "TUGAS UTAMA", "TUGAS UTAMA:", "INSTRUKSI", "INSTRUKSI:"):
            continue
        if s.lower().startswith("kamu adalah "):
            continue
        first_meaningful = s
        break
    target = first_meaningful or clean.splitlines()[0].strip()
    target = re.sub(r"\s+", " ", target)
    return (target[:65] + "...") if len(target) > 68 else target


def compute_side_effect_fingerprint(action: Dict[str, Any], current_url: str = "") -> Optional[str]:
    """
    Menghasilkan sidik jari deterministik untuk aksi berefek samping agar tidak dijalankan dua kali
    tanpa verifikasi hasil sebelumnya (form submit, penghapusan file, klik sensitif, penulisan identik).
    """
    act_name = (action.get("name") or "").lower()
    norm_u = (current_url or "").split("#", 1)[0].strip()

    if act_name == "click":
        # Khusus tombol submit / sensitif akan ditandai oleh orchestrator melalui flag side_effect=True
        if not action.get("_is_side_effect"):
            return None
        raw = f"click_se|{norm_u}|{action.get('index')}|{action.get('_target_sig', '')}"
    elif act_name in ("delete_file", "remove_file", "rmdir", "delete_dir"):
        raw = f"del|{action.get('filename') or action.get('path') or ''}"
    elif act_name == "write_file":
        fn = str(action.get("filename") or "")
        chash = hashlib.sha256(str(action.get("content") or "").encode("utf-8")).hexdigest()[:16]
        raw = f"write|{fn}|{chash}"
    else:
        return None

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class TaskStateStore:
    """
    Penyimpanan status pekerjaan persisten yang:
    - Aman terhadap concurrent writes (threading.RLock + fcntl.flock eksklusif + atomic fsync & os.replace)
    - Aman terhadap proses/server restart (otomatis memulihkan dari .bak & menandai tugas terputus agar siap dilanjutkan)
    - Menyensor seluruh kredensial mentah sebelum menyentuh disk (redact_structure)
    - Mencatat idempotency ledger untuk mencegah duplikasi aksi berefek samping.
    """

    MAX_STORED_TASKS = 40

    def __init__(self, state_dir: str, allow_writes: bool = True, write_block_reason: str = ""):
        self.state_dir = Path(state_dir).resolve()
        self.allow_writes = allow_writes
        self.write_block_reason = (
            write_block_reason
            or "Penulisan task_state diblokir karena penyimpanan persisten tidak tersedia."
        )
        self.main_file = self.state_dir / "tasks_state.json"
        self.bak_file = self.state_dir / "tasks_state.json.bak"
        self.lock_file = self.state_dir / ".tasks_state.lock"
        self._rlock = threading.RLock()
        if self.allow_writes:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._recover_interrupted_on_boot()

    def _with_file_lock(self, write: bool, fn):
        if write and not self.allow_writes:
            raise StorageUnavailableError(self.write_block_reason)
        if not write and not self.allow_writes:
            return fn()
        with self._rlock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with open(self.lock_file, "a+", encoding="utf-8") as lf:
                mode = fcntl.LOCK_EX if write else fcntl.LOCK_SH
                fcntl.flock(lf.fileno(), mode)
                try:
                    return fn()
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> Dict[str, Any]:
        if not self.allow_writes:
            return {"active_task_id": None, "tasks": []}
        for candidate in (self.main_file, self.bak_file):
            if candidate.exists():
                try:
                    raw = candidate.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                        return data
                except Exception:
                    continue
        return {"active_task_id": None, "tasks": []}

    def _write_unlocked(self, state: Dict[str, Any]) -> None:
        if not self.allow_writes:
            raise StorageUnavailableError(self.write_block_reason)
        safe_state = redact_structure(state)
        if len(safe_state.get("tasks", [])) > self.MAX_STORED_TASKS:
            safe_state["tasks"] = safe_state["tasks"][-self.MAX_STORED_TASKS:]

        serialized = json.dumps(safe_state, ensure_ascii=False, indent=2)
        fd, tmp_path = tempfile.mkstemp(prefix=".tsk_", suffix=".tmp", dir=str(self.state_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tf:
                tf.write(serialized)
                tf.flush()
                os.fsync(tf.fileno())
            os.chmod(tmp_path, 0o600)
            if self.main_file.exists():
                try:
                    with open(self.main_file, "rb") as src_f, open(self.bak_file, "wb") as dst_f:
                        dst_f.write(src_f.read())
                        dst_f.flush()
                        os.fsync(dst_f.fileno())
                except Exception:
                    pass
            os.replace(tmp_path, str(self.main_file))
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            raise

    def _recover_interrupted_on_boot(self) -> None:
        def _op():
            st = self._read_unlocked()
            changed = False
            now_iso = datetime.now().isoformat(timespec="seconds")
            for t in st.get("tasks", []):
                if t.get("status") in ("RUNNING", "SEDANG BERJALAN"):
                    t["status"] = "TERHENTI"
                    t["finished_at"] = now_iso
                    if t.get("current_step"):
                        t["current_step"]["status"] = "interrupted"
                    t.setdefault("recovery_log", []).append(
                        {
                            "step_num": len(t.get("completed_steps", [])) + 1,
                            "error_type": "process_restart",
                            "cause": "Proses server/browser mengalami restart saat tugas sedang berjalan.",
                            "strategy": "Checkpoint otomatis disimpan; siap dilanjutkan dari langkah terakhir terverifikasi.",
                            "outcome": "recovered_checkpoint",
                            "timestamp": now_iso,
                        }
                    )
                    t["current_step"] = None
                    changed = True
            if changed:
                st["active_task_id"] = None
                self._write_unlocked(st)

        self._with_file_lock(write=True, fn=_op)

    def create_task(self, instruction: str, initial_plan: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        now_dt = datetime.now()
        task_id = f"TSK-{now_dt.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"
        safe_instr = redact_secrets(instruction)
        planned = [
            {
                "idx": i,
                "step": redact_secrets(p.get("step", "")),
                "status": p.get("status", "pending"),
            }
            for i, p in enumerate(initial_plan or [], 1)
        ]
        record: Dict[str, Any] = {
            "task_id": task_id,
            "title": summarize_task_name(safe_instr),
            "initial_instruction": safe_instr,
            "status": "RUNNING",
            "planned_steps": planned,
            "completed_steps": [],
            "failed_steps": [],
            "current_step": None,
            "started_at": now_dt.isoformat(timespec="seconds"),
            "started_mono": time.monotonic(),
            "finished_at": "",
            "duration_sec": 0,
            "last_verified_url": "",
            "recovery_log": [],
            "issues": [],
            "verification_result": {
                "verified": False,
                "summary": "Belum diverifikasi",
                "details": [],
            },
            "idempotency_ledger": {},
        }

        def _op():
            st = self._read_unlocked()
            # Simpan tanpa field non-permanen bila perlu, namun float monotonic aman untuk sesi yang sama
            st["tasks"].append(record)
            st["active_task_id"] = task_id
            self._write_unlocked(st)
            return dict(record)

        return self._with_file_lock(write=True, fn=_op)

    def update_plan(self, task_id: str, plan: List[Dict[str, str]]) -> None:
        def _op():
            st = self._read_unlocked()
            t = self._find(st, task_id)
            if not t or not plan:
                return
            t["planned_steps"] = [
                {
                    "idx": i,
                    "step": redact_secrets(p.get("step", "")),
                    "status": p.get("status", "pending"),
                }
                for i, p in enumerate(plan, 1)
            ]
            self._write_unlocked(st)

        self._with_file_lock(write=True, fn=_op)

    def mark_step_running(self, task_id: str, step_num: int, action_name: str, description: str) -> None:
        def _op():
            st = self._read_unlocked()
            t = self._find(st, task_id)
            if not t:
                return
            t["status"] = "RUNNING"
            t["current_step"] = {
                "step_num": step_num,
                "action_name": action_name,
                "description": redact_secrets(description)[:220],
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._write_unlocked(st)

        self._with_file_lock(write=True, fn=_op)

    def set_waiting_approval(self, task_id: str, reason: str) -> None:
        def _op():
            st = self._read_unlocked()
            t = self._find(st, task_id)
            if not t:
                return
            t["status"] = "MENUNGGU PERSETUJUAN"
            t["verification_result"]["summary"] = redact_secrets(reason)[:240]
            self._write_unlocked(st)

        self._with_file_lock(write=True, fn=_op)

    def record_step_outcome(
        self,
        task_id: str,
        step_num: int,
        action_name: str,
        result_text: str,
        succeeded: bool,
        verified_url: str = "",
        idempotency_fp: Optional[str] = None,
    ) -> None:
        def _op():
            st = self._read_unlocked()
            t = self._find(st, task_id)
            if not t:
                return
            now_iso = datetime.now().isoformat(timespec="seconds")
            clean_res = redact_secrets(result_text)[:450]
            t["current_step"] = None
            if succeeded:
                t["completed_steps"].append(
                    {
                        "step_num": step_num,
                        "action_name": action_name,
                        "result": clean_res,
                        "timestamp": now_iso,
                    }
                )
                if verified_url and not verified_url.startswith(("about:", "chrome-error://")):
                    t["last_verified_url"] = redact_secrets(verified_url)
                if idempotency_fp:
                    t.setdefault("idempotency_ledger", {})[idempotency_fp] = {
                        "step_num": step_num,
                        "action_name": action_name,
                        "result": clean_res[:240],
                        "verified_at": now_iso,
                    }
            else:
                t["failed_steps"].append(
                    {
                        "step_num": step_num,
                        "action_name": action_name,
                        "error": clean_res,
                        "timestamp": now_iso,
                    }
                )
            self._write_unlocked(st)

        self._with_file_lock(write=True, fn=_op)

    def check_idempotency(self, task_id: str, fingerprint: Optional[str]) -> Optional[Dict[str, Any]]:
        if not fingerprint:
            return None

        def _op():
            st = self._read_unlocked()
            t = self._find(st, task_id)
            if not t:
                return None
            return (t.get("idempotency_ledger") or {}).get(fingerprint)

        return self._with_file_lock(write=False, fn=_op)

    def record_recovery(
        self,
        task_id: str,
        step_num: int,
        error_type: str,
        cause: str,
        strategy: str,
        outcome: str,
    ) -> None:
        def _op():
            st = self._read_unlocked()
            t = self._find(st, task_id)
            if not t:
                return
            t.setdefault("recovery_log", []).append(
                {
                    "step_num": step_num,
                    "error_type": error_type,
                    "cause": redact_secrets(cause)[:240],
                    "strategy": redact_secrets(strategy)[:240],
                    "outcome": redact_secrets(outcome)[:120],
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
            )
            self._write_unlocked(st)

        self._with_file_lock(write=True, fn=_op)

    def finalize_task(
        self,
        task_id: str,
        status: str,
        duration_sec: int,
        verified: bool,
        verification_summary: str,
        verification_details: Optional[List[str]] = None,
        issues: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        norm_status = status.upper().strip()
        if norm_status not in VALID_FINAL_STATUSES:
            norm_status = "BERHASIL" if verified else "GAGAL"

        def _op():
            st = self._read_unlocked()
            t = self._find(st, task_id)
            if not t:
                return {}
            t["status"] = norm_status
            t["current_step"] = None
            t["finished_at"] = datetime.now().isoformat(timespec="seconds")
            t["duration_sec"] = max(1, int(duration_sec))
            t["issues"] = [redact_secrets(x) for x in (issues or [])]
            t["verification_result"] = {
                "verified": bool(verified),
                "summary": redact_secrets(verification_summary)[:400],
                "details": [redact_secrets(d)[:240] for d in (verification_details or [])],
            }
            # Sinkronkan status item rencana berdasarkan status akhir tugas
            for p in t.get("planned_steps", []):
                if norm_status == "BERHASIL" and p.get("status") == "in_progress":
                    p["status"] = "completed"
                elif norm_status in ("GAGAL", "TERHENTI") and p.get("status") == "in_progress":
                    p["status"] = "pending"
            if st.get("active_task_id") == task_id:
                st["active_task_id"] = None
            self._write_unlocked(st)
            return dict(t)

        return self._with_file_lock(write=True, fn=_op)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        def _op():
            st = self._read_unlocked()
            t = self._find(st, task_id)
            return dict(t) if t else None

        return self._with_file_lock(write=False, fn=_op)

    def get_latest_task(self) -> Optional[Dict[str, Any]]:
        def _op():
            st = self._read_unlocked()
            tasks = st.get("tasks") or []
            return dict(tasks[-1]) if tasks else None

        return self._with_file_lock(write=False, fn=_op)

    def get_resumable_context(self) -> Optional[Dict[str, Any]]:
        """Mengambil konteks tugas terakhir yang TERHENTI/SEBAGIAN BERHASIL agar bisa dilanjutkan pasca-restart."""
        t = self.get_latest_task()
        if not t or t.get("status") not in ("TERHENTI", "SEBAGIAN BERHASIL", "MENUNGGU PERSETUJUAN"):
            return None
        return {
            "task_id": t["task_id"],
            "initial_instruction": t["initial_instruction"],
            "last_verified_url": t.get("last_verified_url", ""),
            "completed_steps": t.get("completed_steps", []),
            "idempotency_ledger": t.get("idempotency_ledger", {}),
        }

    @staticmethod
    def _find(state: Dict[str, Any], task_id: str) -> Optional[Dict[str, Any]]:
        for t in state.get("tasks", []):
            if t.get("task_id") == task_id:
                return t
        return None
