import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Set

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.browser_engine import CloudBrowserEngine
from agent.file_manager import WorkspaceFileManager
from agent.github_publisher import GitHubPublisher
from agent.hf_deployer import HuggingFaceDeployer
from agent.llm_providers import LLMConfig, get_shared_http
from agent.orchestrator import AgentOrchestrator
from agent.telegram_bridge import TelegramBotBridge

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
PORT = int(os.getenv("PORT", "8000"))

ws_clients: Set[WebSocket] = set()


async def broadcast_event(payload: Dict[str, Any]) -> None:
    if not ws_clients:
        return
    raw = json.dumps(payload, ensure_ascii=False)
    stale: Set[WebSocket] = set()
    for client in list(ws_clients):
        try:
            await client.send_text(raw)
        except Exception:
            stale.add(client)
    for dead in stale:
        ws_clients.discard(dead)


file_manager = WorkspaceFileManager(os.getenv("WORKSPACE_DIR", str(BASE_DIR / "workspace_files")))
llm_config = LLMConfig()
telegram_bridge = TelegramBotBridge()
github_publisher = GitHubPublisher(repo_dir=str(BASE_DIR), broadcast_cb=broadcast_event)
hf_deployer = HuggingFaceDeployer(base_dir=str(BASE_DIR))
browser_engine = CloudBrowserEngine(
    file_manager=file_manager,
    broadcast_cb=broadcast_event,
    profile_dir=os.getenv("BROWSER_PROFILE_DIR", "/tmp/nusa_operator_profile"),
    internal_port=PORT,
)
orchestrator = AgentOrchestrator(
    browser=browser_engine,
    file_manager=file_manager,
    llm_config=llm_config,
    broadcast_cb=broadcast_event,
    telegram=telegram_bridge,
)
telegram_bridge.attach_orchestrator(orchestrator)


async def _boot_background() -> None:
    await asyncio.sleep(0.1)
    try:
        await telegram_bridge.start_if_env_set()
    except Exception:
        pass
    # Browser Chromium baru dinyalakan saat dibutuhkan (On-Demand) agar 93% RAM bebas untuk bot!


@asynccontextmanager
async def lifespan(app: FastAPI):
    boot_task = asyncio.create_task(_boot_background())
    yield
    boot_task.cancel()
    await telegram_bridge.stop()
    await orchestrator.stop_goal(silent=True)
    await browser_engine.stop()
    try:
        await get_shared_http().aclose()
    except Exception:
        pass


app = FastAPI(title="NusaOperator — Simple Chat Browser Assistant", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ======================== REQUEST SCHEMAS ========================

class GoalPayload(BaseModel):
    goal: str


class TakeoverPayload(BaseModel):
    enabled: bool


class ApprovalPayload(BaseModel):
    decision: str


class NavigatePayload(BaseModel):
    url: str


class SomPayload(BaseModel):
    enabled: bool


class FileSavePayload(BaseModel):
    filename: str
    content: str


class LLMSettingsPayload(BaseModel):
    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""


class TelegramSettingsPayload(BaseModel):
    token: str = ""
    public_web_url: str = ""
    owner_id: int | None = None


LOCKED_PAGE_HTML = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🔒 Layar Browser Pribadi Terkunci</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0b141a;color:#e9edef;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:20px;box-sizing:border-box}
  .card{max-width:380px;width:100%;background:#111b21;border:1px solid #222e35;border-radius:18px;padding:28px 22px;text-align:center;box-shadow:0 12px 32px rgba(0,0,0,.45)}
  .icon{font-size:44px;margin-bottom:10px}
  h1{font-size:19px;margin:0 0 8px}
  p{font-size:14px;color:#8696a0;line-height:1.5;margin:0 0 18px}
  input{width:100%;box-sizing:border-box;padding:12px 14px;border-radius:10px;border:1px solid #2a3942;background:#202c33;color:#fff;font-size:16px;text-align:center;letter-spacing:3px;margin-bottom:12px;outline:none}
  button{width:100%;padding:12px;border:none;border-radius:10px;background:#00a884;color:#081c15;font-weight:700;font-size:15px;cursor:pointer}
</style>
</head>
<body>
  <div class="card">
    <div class="icon">🔒</div>
    <h1>Layar Browser Dikunci Pemilik</h1>
    <p>Buka lewat tombol <b>Layar Live</b> di Bot Telegram pribadi lu, atau masukkan 6 digit PIN Keamanan dari menu <b>🔒 Keamanan</b> di Telegram:</p>
    <form method="GET" action="/">
      <input type="password" inputmode="numeric" name="k" placeholder="PIN 6 Digit" required>
      <button type="submit">Buka Layar Browser</button>
    </form>
  </div>
</body>
</html>"""


def _is_web_authorized(req_or_ws: Any) -> bool:
    if not telegram_bridge.is_locked:
        return True
    expected = telegram_bridge.web_access_key
    q_key = (req_or_ws.query_params.get("k") or "").strip()
    c_key = (req_or_ws.cookies.get("nusa_auth") or "").strip()
    return bool(expected and (q_key == expected or c_key == expected))


@app.middleware("http")
async def private_owner_lock_middleware(request: Request, call_next):
    path = request.url.path
    # Endpoint publik yang tetap dibuka untuk webhook Telegram, static CSS/JS, portal lokal, & healthcheck
    public_paths = ("/api/health", "/api/telegram/webhook", "/demo-portal")
    if path.startswith("/static") or path in public_paths:
        resp = await call_next(request)
        if path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    # Izinkan konfigurasi awal sebelum bot dikunci
    if not telegram_bridge.is_locked:
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

    if not _is_web_authorized(request):
        if path.startswith("/api/"):
            return JSONResponse({"error": "Terkunci khusus pemilik."}, status_code=403)
        return HTMLResponse(LOCKED_PAGE_HTML, status_code=401)

    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    q_key = (request.query_params.get("k") or "").strip()
    if q_key and q_key == telegram_bridge.web_access_key:
        response.set_cookie(
            key="nusa_auth",
            value=telegram_bridge.web_access_key,
            max_age=30 * 86400,
            httponly=True,
            samesite="lax",
        )
    return response


@app.get("/api/health")
async def health_check():
    return {
        "ok": True,
        "telegram_configured": telegram_bridge.is_configured,
        "llm_configured": llm_config.has_api_key,
        "locked": telegram_bridge.is_locked,
    }


class GitHubPushPayload(BaseModel):
    repo_name: str = "asisten-browser"
    private: bool = False


class HfDeployPayload(BaseModel):
    hf_token: str
    telegram_token: str = ""
    gemini_key: str = ""
    space_name: str = "asisten-browser"


# ======================== UI PAGES ========================

@app.get("/", response_class=HTMLResponse)
async def index_page():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/setup", response_class=HTMLResponse)
async def setup_wizard_page():
    return (STATIC_DIR / "setup.html").read_text(encoding="utf-8")


@app.get("/demo-portal", response_class=HTMLResponse)
async def demo_portal_page():
    return (STATIC_DIR / "demo_portal.html").read_text(encoding="utf-8")


# ======================== AGENT API ========================

@app.get("/api/status")
async def get_status():
    return orchestrator.snapshot_state()


@app.post("/api/agent/run")
async def run_agent_goal(payload: GoalPayload):
    await orchestrator.start_goal(payload.goal)
    return {"ok": True}


@app.post("/api/agent/stop")
async def stop_agent_goal():
    await orchestrator.stop_goal()
    return {"ok": True}


@app.post("/api/agent/takeover")
async def set_manual_takeover(payload: TakeoverPayload):
    await orchestrator.toggle_manual_takeover(payload.enabled)
    return {"ok": True}


@app.post("/api/agent/resume")
async def resume_agent():
    await orchestrator.resume_from_takeover()
    return {"ok": True}


@app.post("/api/agent/approval")
async def submit_approval(payload: ApprovalPayload):
    await orchestrator.resolve_approval(payload.decision)
    return {"ok": True}


# ======================== BROWSER CONTROLS API ========================

@app.post("/api/browser/navigate")
async def browser_navigate(payload: NavigatePayload):
    msg = await browser_engine.navigate(payload.url)
    return {"ok": True, "message": msg}


@app.post("/api/browser/back")
async def browser_back():
    await browser_engine.go_back()
    return {"ok": True}


@app.post("/api/browser/forward")
async def browser_forward():
    await browser_engine.go_forward()
    return {"ok": True}


@app.post("/api/browser/reload")
async def browser_reload():
    await browser_engine.reload()
    return {"ok": True}


class CredentialItem(BaseModel):
    selector: str
    value: str


class CredentialFillPayload(BaseModel):
    items: list[CredentialItem] = []
    auto_resume: bool = True


@app.get("/api/browser/login-fields")
async def get_browser_login_fields():
    fields = await browser_engine.get_credential_fields()
    return {"ok": True, "fields": fields, "url": browser_engine._display_url(browser_engine.page.url) if browser_engine.page else ""}


@app.post("/api/browser/fill-credentials")
async def fill_browser_credentials(payload: CredentialFillPayload):
    raw_items = [{"selector": it.selector, "value": it.value} for it in payload.items]
    filled = await browser_engine.fill_credential_fields(
        raw_items,
        captcha_solver=orchestrator.brain.solve_captcha_image,
    )
    if payload.auto_resume:
        await orchestrator.resume_from_takeover()
    return {"ok": True, "filled": filled}


@app.post("/api/browser/som")
async def browser_som_toggle(payload: SomPayload):
    await browser_engine.set_som_badges(payload.enabled)
    return {"ok": True, "som_badges": browser_engine.show_som_badges}


@app.post("/api/browser/reset")
async def browser_reset_session():
    await orchestrator.stop_goal(silent=True)
    orchestrator.chat_messages.clear()
    orchestrator.current_plan.clear()
    await browser_engine.reset_session()
    await orchestrator._sync_state()
    return {"ok": True}


# ======================== WORKSPACE FILES API ========================

@app.get("/api/files")
async def list_workspace_files():
    return {"files": file_manager.list_files()}


@app.get("/api/files/{filename:path}/download")
async def download_workspace_file(filename: str):
    try:
        fpath = file_manager.get_file_path(filename)
        return FileResponse(path=str(fpath), filename=fpath.name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/files/{filename:path}")
async def read_workspace_file(filename: str):
    try:
        content = file_manager.read_file(filename)
        return {"filename": filename, "content": content}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/files")
async def write_workspace_file(payload: FileSavePayload):
    try:
        saved = file_manager.write_file(payload.filename, payload.content)
        await broadcast_event({"type": "files_updated", "files": file_manager.list_files()})
        return {"ok": True, "file": saved}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/files/upload")
async def upload_workspace_file(file: UploadFile = File(...)):
    data = await file.read()
    saved = file_manager.save_bytes(file.filename or "uploaded_file", data)
    await broadcast_event({"type": "files_updated", "files": file_manager.list_files()})
    await orchestrator._push_chat(
        "file",
        f"File diupload: {saved['name']}",
        {"filename": saved["name"], "size": saved["size"]},
    )
    return {"ok": True, "file": saved}


@app.delete("/api/files/{filename:path}")
async def remove_workspace_file(filename: str):
    file_manager.delete_file(filename)
    await broadcast_event({"type": "files_updated", "files": file_manager.list_files()})
    return {"ok": True}


# ======================== SETTINGS API ========================

@app.post("/api/settings/llm")
async def update_llm_settings(payload: LLMSettingsPayload):
    llm_config.update(
        provider=payload.provider,
        model=payload.model,
        api_key=payload.api_key,
        base_url=payload.base_url,
    )
    return {"ok": True, "llm": llm_config.public_dict()}


@app.post("/api/settings/telegram")
async def update_telegram_settings(payload: TelegramSettingsPayload):
    try:
        info = await telegram_bridge.configure(payload.token, payload.public_web_url, payload.owner_id)
        return {"ok": True, "telegram": info}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/github/start")
async def start_github_push(payload: GitHubPushPayload):
    try:
        status = await github_publisher.start_device_flow(payload.repo_name, payload.private)
        return {"ok": True, **status}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/github/status")
async def get_github_push_status():
    return github_publisher.last_status


@app.post("/api/deploy/hf")
async def deploy_to_huggingface(payload: HfDeployPayload):
    try:
        res = await hf_deployer.deploy_24_7(
            hf_token=payload.hf_token,
            telegram_token=payload.telegram_token,
            gemini_key=payload.gemini_key,
            space_name=payload.space_name,
        )
        return {"ok": True, **res}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/telegram/webhook")
async def telegram_webhook_endpoint(request: Request):
    sec_hdr = request.headers.get("x-telegram-bot-api-secret-token")
    if not telegram_bridge.verify_webhook_request(sec_hdr):
        raise HTTPException(status_code=403, detail="Invalid webhook secret token")
    upd = await request.json()
    await telegram_bridge.process_webhook_update(upd)
    return {"ok": True}


# ======================== REAL-TIME WEBSOCKET ========================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not _is_web_authorized(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    ws_clients.add(websocket)
    await browser_engine.set_viewer_count(len(ws_clients))

    # Selalu ambil tangkapan layar paling segar saat user membuka Live CCTV (jangan pernah kirim frame lama yang masih kosong!)
    if browser_engine._started and browser_engine.page and not browser_engine.page.is_closed():
        cur_u = browser_engine.page.url or ""
        if cur_u and cur_u != "about:blank":
            await browser_engine.refresh_frame()

    await websocket.send_text(
        json.dumps(
            {
                "type": "init",
                "snapshot": orchestrator.snapshot_state(),
                "frame": browser_engine.latest_frame_b64 or browser_engine.last_real_frame_b64,
            },
            ensure_ascii=False,
        )
    )

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            ev_type = data.get("type")

            if ev_type == "human_click":
                await browser_engine.human_click(
                    x=float(data.get("x", 0)),
                    y=float(data.get("y", 0)),
                    button=str(data.get("button", "left")),
                    click_count=int(data.get("click_count", 1)),
                )
            elif ev_type == "human_wheel":
                await browser_engine.human_wheel(
                    dx=float(data.get("dx", 0)),
                    dy=float(data.get("dy", 0)),
                )
            elif ev_type == "human_key":
                await browser_engine.human_key(str(data.get("key", "")))
            elif ev_type == "human_type":
                await browser_engine.human_type(str(data.get("text", "")))
    except WebSocketDisconnect:
        ws_clients.discard(websocket)
        await browser_engine.set_viewer_count(len(ws_clients))
    except Exception:
        ws_clients.discard(websocket)
        await browser_engine.set_viewer_count(len(ws_clients))


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        loop="uvloop",
        http="httptools",
        access_log=False,
        log_level="warning",
    )
