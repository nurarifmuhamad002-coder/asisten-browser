import asyncio
import base64
import os
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

from playwright.async_api import BrowserContext, CDPSession, Download, Page, Playwright, async_playwright

from agent.dom_inspector import clear_som_badges, inspect_page
from agent.file_manager import WorkspaceFileManager


class CloudBrowserEngine:
    """
    Mesin Cloud Browser berbasis Playwright Chromium + CDP Screencast.
    Mendukung:
    - Live video/frame streaming ke Web UI via WebSocket
    - Eksekusi aksi oleh AI Agent (navigasi, klik, ketik, pilih dropdown, scroll)
    - Human Takeover real-time (klik mouse, scroll, ketik keyboard langsung oleh user)
    - Persistent session/cookies agar login akun user tersimpan aman
    - Tangkap otomatis file download browser ke folder workspace_files
    """

    VIEWPORT_WIDTH = 1280
    VIEWPORT_HEIGHT = 800

    def __init__(
        self,
        file_manager: WorkspaceFileManager,
        broadcast_cb: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]],
        profile_dir: str = "/tmp/nusa_operator_profile",
        internal_port: int = 8000,
    ):
        self.file_manager = file_manager
        self.broadcast = broadcast_cb
        self.profile_dir = Path(profile_dir)
        self.internal_port = internal_port

        self._pw: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._cdp: Optional[CDPSession] = None

        self.latest_frame_b64: str = ""
        self.show_som_badges: bool = False
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self, start_url: Optional[str] = None) -> None:
        if self._started:
            return
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        downloads_tmp = Path("/tmp/nusa_downloads")
        downloads_tmp.mkdir(parents=True, exist_ok=True)

        self._pw = await async_playwright().start()
        self.context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=True,
            viewport={"width": self.VIEWPORT_WIDTH, "height": self.VIEWPORT_HEIGHT},
            accept_downloads=True,
            downloads_path=str(downloads_tmp),
            locale="id-ID",
            timezone_id="Asia/Jakarta",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        self.context.on("page", lambda new_page: asyncio.create_task(self._on_new_page(new_page)))

        if self.context.pages:
            self.page = self.context.pages[0]
        else:
            self.page = await self.context.new_page()

        await self._attach_page(self.page)
        self._started = True

        initial_target = start_url or f"http://127.0.0.1:{self.internal_port}/demo-portal"
        try:
            await self.navigate(initial_target)
        except Exception:
            pass

    async def stop(self) -> None:
        self._started = False
        try:
            if self._cdp:
                await self._cdp.detach()
        except Exception:
            pass
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    async def reset_session(self) -> None:
        """Menghapus cookies & storage browser jika user ingin logout dari semua akun."""
        if not self.context:
            return
        await self.context.clear_cookies()
        if self.page:
            try:
                await self.page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
            except Exception:
                pass
            await self.navigate(f"http://127.0.0.1:{self.internal_port}/demo-portal")

    async def _on_new_page(self, new_page: Page) -> None:
        try:
            await new_page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        self.page = new_page
        await self._attach_page(new_page)
        await self.refresh_frame()

    async def _attach_page(self, page: Page) -> None:
        page.on("download", lambda dl: asyncio.create_task(self._handle_download(dl)))
        page.on("framenavigated", lambda frame: asyncio.create_task(self._on_navigated(frame)))

        if self._cdp:
            try:
                await self._cdp.detach()
            except Exception:
                pass

        try:
            self._cdp = await page.context.new_cdp_session(page)
            self._cdp.on(
                "Page.screencastFrame",
                lambda event: asyncio.create_task(self._on_screencast_frame(event)),
            )
            await self._cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 72,
                    "maxWidth": self.VIEWPORT_WIDTH,
                    "maxHeight": self.VIEWPORT_HEIGHT,
                    "everyNthFrame": 1,
                },
            )
        except Exception:
            self._cdp = None

    async def _on_navigated(self, frame: Any) -> None:
        if self.page and frame == self.page.main_frame:
            await self._broadcast_page_meta()

    async def _on_screencast_frame(self, event: Dict[str, Any]) -> None:
        session_id = event.get("sessionId")
        data_b64 = event.get("data", "")
        if self._cdp and session_id is not None:
            try:
                await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
            except Exception:
                pass
        if data_b64:
            self.latest_frame_b64 = data_b64
            await self.broadcast(
                {
                    "type": "frame",
                    "data": data_b64,
                    "url": self._display_url(self.page.url if self.page else ""),
                    "title": await self._safe_title(),
                }
            )

    async def _handle_download(self, download: Download) -> None:
        try:
            suggested = download.suggested_filename or "downloaded_file"
            temp_path = await download.path()
            if temp_path and os.path.exists(temp_path):
                raw_bytes = Path(temp_path).read_bytes()
                saved = self.file_manager.save_bytes(suggested, raw_bytes)
                await self.broadcast(
                    {
                        "type": "file_downloaded",
                        "filename": saved["name"],
                        "size": saved["size"],
                        "files": self.file_manager.list_files(),
                    }
                )
        except Exception as exc:
            await self.broadcast(
                {"type": "log", "level": "warning", "message": f"Gagal menangkap file download: {exc}"}
            )

    def _normalize_url(self, raw_url: str) -> str:
        url = (raw_url or "").strip()
        if not url:
            return f"http://127.0.0.1:{self.internal_port}/demo-portal"
        if url.startswith("/demo-portal") or url.startswith("demo-portal"):
            suffix = url[len("demo-portal"):] if url.startswith("demo-portal") else url[len("/demo-portal"):]
            return f"http://127.0.0.1:{self.internal_port}/demo-portal{suffix}"
        if not url.startswith(("http://", "https://", "about:")):
            # Jika terlihat seperti domain, tambahkan https://; jika kalimat pencarian, arahkan ke DuckDuckGo HTML
            if "." in url and " " not in url:
                return f"https://{url}"
            from urllib.parse import quote_plus
            return f"https://html.duckduckgo.com/html/?q={quote_plus(url)}"
        return url

    def _display_url(self, url: str) -> str:
        local_prefix = f"http://127.0.0.1:{self.internal_port}"
        if url.startswith(local_prefix):
            return url[len(local_prefix):] or "/"
        return url

    async def _safe_title(self) -> str:
        if not self.page:
            return ""
        try:
            return await self.page.title()
        except Exception:
            return ""

    async def _broadcast_page_meta(self) -> None:
        if not self.page:
            return
        await self.broadcast(
            {
                "type": "page_meta",
                "url": self._display_url(self.page.url),
                "title": await self._safe_title(),
            }
        )

    async def refresh_frame(self) -> str:
        """Mengambil screenshot terbaru secara eksplisit dan mengirimkannya ke semua klien UI."""
        if not self.page:
            return self.latest_frame_b64
        try:
            if self.show_som_badges:
                await inspect_page(self.page, show_badges=True)
            else:
                await clear_som_badges(self.page)
            raw = await self.page.screenshot(type="jpeg", quality=72)
            self.latest_frame_b64 = base64.b64encode(raw).decode("ascii")
            await self.broadcast(
                {
                    "type": "frame",
                    "data": self.latest_frame_b64,
                    "url": self._display_url(self.page.url),
                    "title": await self._safe_title(),
                }
            )
        except Exception:
            pass
        return self.latest_frame_b64

    async def set_som_badges(self, enabled: bool) -> None:
        self.show_som_badges = enabled
        await self.refresh_frame()

    async def observe(self, annotated_screenshot: bool = True) -> Dict[str, Any]:
        """
        Membaca kondisi halaman secara menyeluruh (DOM + elemen interaktif + screenshot Vision).
        """
        if not self.page:
            raise RuntimeError("Browser belum siap.")
        dom_state = await inspect_page(self.page, show_badges=(annotated_screenshot or self.show_som_badges))
        screenshot_b64 = ""
        try:
            raw = await self.page.screenshot(type="jpeg", quality=70)
            screenshot_b64 = base64.b64encode(raw).decode("ascii")
            self.latest_frame_b64 = screenshot_b64
        except Exception:
            screenshot_b64 = self.latest_frame_b64

        if annotated_screenshot and not self.show_som_badges:
            await clear_som_badges(self.page)

        dom_state["url_display"] = self._display_url(dom_state.get("url", ""))
        dom_state["screenshot_b64"] = screenshot_b64
        return dom_state

    async def emit_cursor(self, x: int, y: int, label: str = "", kind: str = "click") -> None:
        await self.broadcast(
            {
                "type": "agent_cursor",
                "x": x,
                "y": y,
                "label": label,
                "kind": kind,
            }
        )

    # ======================== AGENT ACTIONS ========================

    async def navigate(self, url: str) -> str:
        if not self.page:
            raise RuntimeError("Browser belum aktif.")
        target = self._normalize_url(url)
        await self.page.goto(target, wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(0.45)
        await self.refresh_frame()
        return f"Berhasil membuka {self._display_url(self.page.url)} ({await self._safe_title()})"

    async def click_element(self, index: int, elements: List[Dict[str, Any]]) -> str:
        if not self.page:
            raise RuntimeError("Browser belum aktif.")
        target_el = next((e for e in elements if e["index"] == index), None)
        selector = f'[data-nusa-idx="{index}"]'

        if target_el:
            cx = target_el["rect"]["centerX"]
            cy = target_el["rect"]["centerY"]
            lbl = target_el.get("text") or target_el.get("label") or target_el.get("placeholder") or f"#{index}"
            await self.emit_cursor(cx, cy, f"Klik [{index}] {lbl[:28]}", "click")
            await asyncio.sleep(0.25)

        locator = self.page.locator(selector).first
        try:
            if await locator.count() > 0:
                await locator.scroll_into_view_if_needed(timeout=3000)
                await locator.click(timeout=5000)
            elif target_el:
                await self.page.mouse.click(target_el["rect"]["centerX"], target_el["rect"]["centerY"])
            else:
                return f"Elemen [{index}] tidak ditemukan di halaman."
        except Exception:
            if target_el:
                await self.page.mouse.click(target_el["rect"]["centerX"], target_el["rect"]["centerY"])
            else:
                raise

        await asyncio.sleep(0.55)
        await self.refresh_frame()
        desc = (target_el.get("text") or target_el.get("label") or f"elemen [{index}]") if target_el else f"elemen [{index}]"
        return f"Berhasil klik [{index}] '{desc[:50]}'"

    async def type_into_element(
        self,
        index: int,
        text: str,
        elements: List[Dict[str, Any]],
        press_enter: bool = False,
        clear_first: bool = True,
    ) -> str:
        if not self.page:
            raise RuntimeError("Browser belum aktif.")
        target_el = next((e for e in elements if e["index"] == index), None)
        selector = f'[data-nusa-idx="{index}"]'

        if target_el:
            cx = target_el["rect"]["centerX"]
            cy = target_el["rect"]["centerY"]
            await self.emit_cursor(cx, cy, f"Ketik '{text[:22]}'", "type")
            await asyncio.sleep(0.2)

        locator = self.page.locator(selector).first
        if await locator.count() > 0:
            await locator.scroll_into_view_if_needed(timeout=3000)
            await locator.click(timeout=4000)
            if clear_first:
                try:
                    await locator.fill(text, timeout=4000)
                except Exception:
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                    await self.page.keyboard.insert_text(text)
            else:
                await self.page.keyboard.insert_text(text)
        elif target_el:
            await self.page.mouse.click(target_el["rect"]["centerX"], target_el["rect"]["centerY"])
            if clear_first:
                await self.page.keyboard.press("Control+A")
                await self.page.keyboard.press("Backspace")
            await self.page.keyboard.insert_text(text)
        else:
            return f"Kolom input [{index}] tidak ditemukan."

        if press_enter:
            await asyncio.sleep(0.15)
            await self.page.keyboard.press("Enter")
            await asyncio.sleep(0.65)
        else:
            await asyncio.sleep(0.3)

        await self.refresh_frame()
        return f"Mengetik '{text}' pada kolom [{index}]" + (" lalu tekan Enter." if press_enter else ".")

    async def select_dropdown(self, index: int, option_value_or_text: str, elements: List[Dict[str, Any]]) -> str:
        if not self.page:
            raise RuntimeError("Browser belum aktif.")
        target_el = next((e for e in elements if e["index"] == index), None)
        selector = f'[data-nusa-idx="{index}"]'
        if target_el:
            await self.emit_cursor(
                target_el["rect"]["centerX"],
                target_el["rect"]["centerY"],
                f"Pilih '{option_value_or_text[:22]}'",
                "select",
            )
        locator = self.page.locator(selector).first
        if await locator.count() == 0:
            return f"Dropdown [{index}] tidak ditemukan."

        try:
            await locator.select_option(label=option_value_or_text, timeout=3000)
        except Exception:
            try:
                await locator.select_option(value=option_value_or_text, timeout=3000)
            except Exception:
                # Partial match fallback
                opts = (target_el or {}).get("options", [])
                matched = next(
                    (
                        o["value"]
                        for o in opts
                        if option_value_or_text.lower() in o["text"].lower()
                        or option_value_or_text.lower() in o["value"].lower()
                    ),
                    None,
                )
                if matched is not None:
                    await locator.select_option(value=matched, timeout=3000)
                else:
                    return f"Opsi '{option_value_or_text}' tidak ditemukan pada dropdown [{index}]."

        await asyncio.sleep(0.3)
        await self.refresh_frame()
        return f"Memilih opsi '{option_value_or_text}' pada dropdown [{index}]."

    async def scroll_page(self, direction: str = "down", amount: int = 520) -> str:
        if not self.page:
            raise RuntimeError("Browser belum aktif.")
        d = direction.lower()
        if d == "top":
            await self.page.evaluate("() => window.scrollTo({top: 0, behavior: 'instant'})")
        elif d == "bottom":
            await self.page.evaluate("() => window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'})")
        elif d == "up":
            await self.page.mouse.wheel(0, -abs(amount))
        else:
            await self.page.mouse.wheel(0, abs(amount))
        await asyncio.sleep(0.35)
        await self.refresh_frame()
        return f"Scroll halaman ke arah '{direction}'."

    async def go_back(self) -> str:
        if not self.page:
            return "Browser belum aktif."
        await self.page.go_back(wait_until="domcontentloaded", timeout=10000)
        await asyncio.sleep(0.35)
        await self.refresh_frame()
        return f"Kembali ke halaman sebelumnya ({self._display_url(self.page.url)})."

    async def go_forward(self) -> str:
        if not self.page:
            return "Browser belum aktif."
        await self.page.go_forward(wait_until="domcontentloaded", timeout=10000)
        await asyncio.sleep(0.35)
        await self.refresh_frame()
        return f"Maju ke halaman ({self._display_url(self.page.url)})."

    async def reload(self) -> str:
        if not self.page:
            return "Browser belum aktif."
        await self.page.reload(wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(0.35)
        await self.refresh_frame()
        return "Halaman dimuat ulang."

    # ======================== HUMAN LIVE TAKEOVER INPUTS ========================

    async def human_click(self, x: float, y: float, button: str = "left", click_count: int = 1) -> None:
        if not self.page:
            return
        px = max(0, min(self.VIEWPORT_WIDTH - 1, int(round(x))))
        py = max(0, min(self.VIEWPORT_HEIGHT - 1, int(round(y))))
        btn = button if button in ("left", "right", "middle") else "left"
        await self.page.mouse.click(px, py, button=btn, click_count=click_count)
        await asyncio.sleep(0.12)
        await self.refresh_frame()

    async def human_wheel(self, dx: float, dy: float) -> None:
        if not self.page:
            return
        await self.page.mouse.wheel(int(dx), int(dy))
        await asyncio.sleep(0.08)
        await self.refresh_frame()

    async def human_key(self, key: str) -> None:
        """Meneruskan tombol keyboard dari user langsung ke Chromium tanpa pernah mencatat nilainya."""
        if not self.page or not key:
            return
        await self.page.keyboard.press(key)
        await asyncio.sleep(0.08)
        await self.refresh_frame()

    async def human_type(self, text: str) -> None:
        """Meneruskan teks ketikan/paste user langsung ke Chromium (aman untuk password/OTP)."""
        if not self.page or not text:
            return
        await self.page.keyboard.insert_text(text)
        await asyncio.sleep(0.08)
        await self.refresh_frame()
