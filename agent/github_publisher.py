import asyncio
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional

import httpx

GH_PUBLIC_CLIENT_ID = "178c6fc778ccc68e1d6a"


class GitHubPublisher:
    """
    Membuat repository GitHub baru dan meng-upload seluruh kode secara otomatis
    menggunakan GitHub Device Flow (user cukup memasukkan kode 8 digit di browser HP,
    tanpa perlu membuat Personal Access Token manual atau download/ekstrak file).
    """

    def __init__(
        self,
        repo_dir: str,
        broadcast_cb: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]],
    ):
        self.repo_dir = Path(repo_dir).resolve()
        self.broadcast = broadcast_cb
        self._task: Optional[asyncio.Task] = None
        self.last_status: Dict[str, Any] = {"stage": "idle"}

    async def start_device_flow(self, repo_name: str, private: bool = False) -> Dict[str, Any]:
        clean_repo = re.sub(r"[^a-zA-Z0-9._\-]", "-", (repo_name or "asisten-browser").strip()).strip("-")
        if not clean_repo:
            clean_repo = "asisten-browser"

        if self._task and not self._task.done():
            self._task.cancel()

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://github.com/login/device/code",
                headers={"Accept": "application/json"},
                data={"client_id": GH_PUBLIC_CLIENT_ID, "scope": "repo"},
            )
            resp.raise_for_status()
            data = resp.json()

        user_code = data["user_code"]
        verification_uri = data.get("verification_uri", "https://github.com/login/device")
        device_code = data["device_code"]
        interval = int(data.get("interval", 5))
        expires_in = int(data.get("expires_in", 899))

        self.last_status = {
            "stage": "waiting_user",
            "user_code": user_code,
            "verification_uri": verification_uri,
            "repo_name": clean_repo,
            "private": private,
        }
        try:
            import json as _json
            (self.repo_dir / ".gh_device_state.json").write_text(
                _json.dumps(
                    {
                        "device_code": device_code,
                        "user_code": user_code,
                        "repo_name": clean_repo,
                        "private": private,
                    }
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

        self._task = asyncio.create_task(
            self._poll_and_push(
                device_code=device_code,
                interval=interval,
                expires_in=expires_in,
                repo_name=clean_repo,
                private=private,
            )
        )
        return self.last_status

    async def _poll_and_push(
        self,
        device_code: str,
        interval: int,
        expires_in: int,
        repo_name: str,
        private: bool,
    ) -> None:
        wait_sec = max(5, interval + 1)
        elapsed = 0
        access_token = ""

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                while elapsed < expires_in:
                    await asyncio.sleep(wait_sec)
                    elapsed += wait_sec

                    tok_resp = await client.post(
                        "https://github.com/login/oauth/access_token",
                        headers={"Accept": "application/json"},
                        data={
                            "client_id": GH_PUBLIC_CLIENT_ID,
                            "device_code": device_code,
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        },
                    )
                    tok_data = tok_resp.json()
                    err = tok_data.get("error")

                    if "access_token" in tok_data:
                        access_token = tok_data["access_token"]
                        break
                    if err == "authorization_pending":
                        continue
                    if err == "slow_down":
                        wait_sec += 5
                        continue
                    raise RuntimeError(f"Otorisasi GitHub gagal atau dibatalkan ({err}).")

                if not access_token:
                    raise RuntimeError("Waktu kode 8-digit habis. Silakan klik buat kode baru.")

                await self._emit(
                    {
                        "stage": "pushing",
                        "message": "✅ Login GitHub diterima! Lagi bikin repository & upload semua kode...",
                    }
                )

                gh_headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }

                # 1. Ambil info akun GitHub user
                me_resp = await client.get("https://api.github.com/user", headers=gh_headers)
                me_resp.raise_for_status()
                owner = me_resp.json()["login"]

                # 2. Buat repository baru (jika belum ada)
                create_resp = await client.post(
                    "https://api.github.com/user/repos",
                    headers=gh_headers,
                    json={
                        "name": repo_name,
                        "private": private,
                        "description": "Asisten Browser AI — Kontrol lewat Chat Simpel + Human-in-the-Loop Login & Approval",
                        "auto_init": False,
                    },
                )
                if create_resp.status_code not in (200, 201, 422):
                    raise RuntimeError(f"Gagal membuat repo di GitHub: {create_resp.text[:250]}")

                repo_url = f"https://github.com/{owner}/{repo_name}"

                # 3. Commit file terbaru & push aman tanpa menyimpan token di .git/config
                await asyncio.to_thread(self._git_commit_and_push, owner, repo_name, access_token)
                try:
                    st_file = self.repo_dir / ".gh_device_state.json"
                    if st_file.exists():
                        st_file.unlink()
                except Exception:
                    pass

                result = {
                    "stage": "done",
                    "repo_url": repo_url,
                    "codespace_url": f"https://codespaces.new/{owner}/{repo_name}",
                    "owner": owner,
                    "repo_name": repo_name,
                    "message": f"🎉 Beres! Semua kode udah masuk ke {repo_url}",
                }
                await self._emit(result)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            await self._emit({"stage": "error", "message": f"❌ Gagal upload ke GitHub: {exc}"})

    def _git_commit_and_push(self, owner: str, repo_name: str, token: str) -> None:
        cwd = str(self.repo_dir)
        subprocess.run(["git", "add", "."], cwd=cwd, check=False)
        subprocess.run(
            ["git", "commit", "-m", "Update Asisten Browser AI"],
            cwd=cwd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        clean_remote = f"https://github.com/{owner}/{repo_name}.git"
        subprocess.run(["git", "remote", "remove", "origin"], cwd=cwd, check=False, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "remote", "add", "origin", clean_remote], cwd=cwd, check=True)

        askpass_path = Path("/tmp/gh_ephemeral_askpass.sh")
        try:
            askpass_path.write_text(
                '#!/bin/sh\n'
                'case "$1" in\n'
                '  *Username*) echo "x-access-token" ;;\n'
                '  *) echo "$GH_PUSH_TOKEN" ;;\n'
                'esac\n',
                encoding="utf-8",
            )
            askpass_path.chmod(stat.S_IRWXU)

            env = os.environ.copy()
            env["GIT_ASKPASS"] = str(askpass_path)
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GH_PUSH_TOKEN"] = token

            res = subprocess.run(
                ["git", "push", "-u", "--force", "origin", "HEAD:refs/heads/main"],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
            if res.returncode != 0:
                raise RuntimeError(res.stderr.strip()[:250] or "git push gagal")
        finally:
            if askpass_path.exists():
                askpass_path.unlink()

    async def _emit(self, status_dict: Dict[str, Any]) -> None:
        self.last_status = status_dict
        await self.broadcast({"type": "github_status", **status_dict})
