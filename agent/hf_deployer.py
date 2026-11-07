import asyncio
import re
from pathlib import Path
from typing import Any, Dict

import httpx


class HuggingFaceDeployer:
    """
    Membuat server Cloud 24 Jam gratis (16 GB RAM Docker Space di Hugging Face),
    menyimpan TELEGRAM_BOT_TOKEN & GEMINI_API_KEY secara terenkripsi di Space Secrets,
    dan meng-upload seluruh kode otomatis dalam satu klik dari HP.
    """

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).resolve()

    async def deploy_24_7(
        self,
        hf_token: str,
        telegram_token: str = "",
        gemini_key: str = "",
        space_name: str = "asisten-browser",
    ) -> Dict[str, Any]:
        clean_hf = (hf_token or "").strip()
        clean_tg = (telegram_token or "").strip()
        clean_gem = (gemini_key or "").strip()
        clean_space = re.sub(r"[^a-zA-Z0-9\-]", "-", (space_name or "asisten-browser").strip()).strip("-").lower()
        if not clean_space:
            clean_space = "asisten-browser"

        if not clean_hf.startswith("hf_"):
            raise ValueError("Token Hugging Face harus diawali dengan 'hf_...'. Pastikan membuat token tipe Write.")

        bot_username = ""
        if clean_tg:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.get(f"https://api.telegram.org/bot{clean_tg}/getMe")
                d = r.json()
                if not d.get("ok"):
                    raise ValueError("Token Bot Telegram tidak valid. Cek lagi hasil copy dari @BotFather.")
                bot_username = d["result"].get("username", "")

        result = await asyncio.to_thread(
            self._sync_deploy,
            clean_hf,
            clean_tg,
            clean_gem,
            clean_space,
            bot_username,
        )
        return result

    def _sync_deploy(
        self,
        hf_token: str,
        telegram_token: str,
        gemini_key: str,
        space_name: str,
        bot_username: str,
    ) -> Dict[str, Any]:
        from huggingface_hub import HfApi

        api = HfApi(token=hf_token)
        try:
            who = api.whoami()
            hf_user = who["name"]
        except Exception as exc:
            raise ValueError(f"Token Hugging Face ditolak: {exc}") from exc

        repo_id = f"{hf_user}/{space_name}"
        direct_app_url = f"https://{hf_user.lower()}-{space_name.lower()}.hf.space"
        space_dashboard_url = f"https://huggingface.co/spaces/{repo_id}"

        # 1. Buat Docker Space (gratis 2 vCPU + 16 GB RAM)
        api.create_repo(
            repo_id=repo_id,
            repo_type="space",
            space_sdk="docker",
            private=False,
            exist_ok=True,
        )

        # 2. Simpan Secret rahasia secara terenkripsi di server Hugging Face
        api.add_space_secret(repo_id=repo_id, key="PUBLIC_WEB_URL", value=direct_app_url)
        if telegram_token:
            api.add_space_secret(repo_id=repo_id, key="TELEGRAM_BOT_TOKEN", value=telegram_token)
        if gemini_key:
            api.add_space_secret(repo_id=repo_id, key="GEMINI_API_KEY", value=gemini_key)
            api.add_space_secret(repo_id=repo_id, key="LLM_PROVIDER", value="gemini")
            api.add_space_secret(repo_id=repo_id, key="LLM_MODEL", value="gemini-2.5-flash")

        # 3. Upload seluruh file proyek bersih sekaligus
        api.upload_folder(
            repo_id=repo_id,
            repo_type="space",
            folder_path=str(self.base_dir),
            commit_message="Deploy Asisten Browser 24/7 + Telegram Webhook",
            allow_patterns=[
                "main.py",
                "requirements.txt",
                "Dockerfile",
                "README.md",
                ".env.example",
                "agent/*.py",
                "static/*",
                "workspace_files/profil_pengajuan.json",
                "workspace_files/catatan_workspace.md",
            ],
        )

        return {
            "hf_user": hf_user,
            "repo_id": repo_id,
            "app_url": direct_app_url,
            "space_url": space_dashboard_url,
            "bot_username": bot_username,
            "telegram_url": f"https://t.me/{bot_username}" if bot_username else "",
        }
