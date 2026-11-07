import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Set

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.browser_engine import CloudBrowserEngine
from agent.file_manager import WorkspaceFileManager
from agent.github_publisher import GitHubPublisher
from agent.llm_providers import LLMConfig
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
    await asyncio.sleep(0.4)
    await browser_engine.start(f"http://127.0.0.1:{PORT}/demo-portal")
    await telegram_bridge.start_if_env_set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    boot_task = asyncio.create_task(_boot_background())
    yield
    boot_task.cancel()
    await telegram_bridge.stop()
    await orchestrator.stop_goal(silent=True)
    await browser_engine.stop()


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


class GitHubPushPayload(BaseModel):
    repo_name: str = "asisten-browser"
    private: bool = False


# ======================== UI PAGES ========================

@app.get("/", response_class=HTMLResponse)
async def index_page():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


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


@app.get("/api/files/{filename}")
async def read_workspace_file(filename: str):
    try:
        content = file_manager.read_file(filename)
        return {"filename": filename, "content": content}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/files/{filename}/download")
async def download_workspace_file(filename: str):
    try:
        fpath = file_manager.get_file_path(filename)
        return FileResponse(path=str(fpath), filename=fpath.name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


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


@app.delete("/api/files/{filename}")
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
        info = await telegram_bridge.configure(payload.token, payload.public_web_url)
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


# ======================== REAL-TIME WEBSOCKET ========================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)

    if not browser_engine.latest_frame_b64 and browser_engine.page:
        await browser_engine.refresh_frame()

    await websocket.send_text(
        json.dumps(
            {
                "type": "init",
                "snapshot": orchestrator.snapshot_state(),
                "frame": browser_engine.latest_frame_b64,
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
    except Exception:
        ws_clients.discard(websocket)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
