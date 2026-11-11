import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

from playwright.async_api import Browser, BrowserContext, CDPSession, Download, Page, Playwright, async_playwright

from agent.dom_inspector import clear_som_badges, inspect_page
from agent.file_manager import WorkspaceFileManager
from agent.security import redact_secrets, register_runtime_secret


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

    VIEWPORT_WIDTH = 800
    VIEWPORT_HEIGHT = 600

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
        self.state_file = self.profile_dir / "storage_state.json"
        self.proxy_file = self.profile_dir / "proxy_config.json"
        self.internal_port = internal_port
        self.proxy_config: Optional[Dict[str, str]] = self._load_saved_proxy()

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._cdp: Optional[CDPSession] = None

        self.latest_frame_b64: str = ""
        self.last_real_frame_b64: str = ""
        self.last_real_url: str = ""
        self._last_screen_file = self.profile_dir / "last_screen.b64"
        self._last_url_file = self.profile_dir / "last_url.txt"
        self._load_persisted_screen()
        self._cached_title: str = ""
        self.show_som_badges: bool = False
        self._lock = asyncio.Lock()
        self._started = False
        self._viewer_count: int = 0
        self._screencast_on: bool = False
        self._switching_tab: bool = False
        self._last_active: float = 0.0
        self._idle_task: Optional[asyncio.Task] = None

    def _load_persisted_screen(self) -> None:
        try:
            if self._last_screen_file.exists():
                b64 = self._last_screen_file.read_text(encoding="ascii").strip()
                if b64:
                    self.last_real_frame_b64 = b64
                    self.latest_frame_b64 = b64
            if self._last_url_file.exists():
                u = self._last_url_file.read_text(encoding="utf-8").strip()
                if u and u != "about:blank":
                    self.last_real_url = u
        except Exception:
            pass

    def _persist_last_screen(self, b64: str, url: str) -> None:
        if not b64 or not url or url.startswith(("about:", "chrome-error://")):
            return
        self.last_real_frame_b64 = b64
        self.last_real_url = url
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._last_screen_file.write_text(b64, encoding="ascii")
            self._last_url_file.write_text(url, encoding="utf-8")
        except Exception:
            pass

    def _touch(self) -> None:
        import time as _t
        self._last_active = _t.monotonic()

    @staticmethod
    def parse_proxy_input(raw: str) -> Optional[Dict[str, str]]:
        """
        Menerima SEMUA format kode proxy dari penyedia Proxy Indonesia:
        1) http://user:pass@host:port atau socks5://user:pass@host:port
        2) host:port:user:pass  (format populer toko proxy Indo)
        3) user:pass@host:port
        4) host:port
        """
        s = (raw or "").strip()
        if not s or s.lower() in ("off", "mati", "hapus", "none", "reset", "matikan"):
            return None

        from urllib.parse import urlparse, unquote

        # Jika belum ada skema tapi memakai '@', tambahkan http://
        if "://" not in s and "@" in s:
            s = "http://" + s

        cdn_bypass = "*.cloudflare.com,unpkg.com,*.unpkg.com,*.googleapis.com,*.gstatic.com,*.jsdelivr.net"

        def _enrich_id_proxy(c: Dict[str, str]) -> Dict[str, str]:
            c["bypass"] = cdn_bypass
            # Jika user menempel kode mentah WaroengProxy tanpa kode negara, otomatis kunci ke IP Indonesia Sticky!
            srv_l = c.get("server", "").lower()
            usr = c.get("username", "")
            if "waroengproxy.com" in srv_l and usr and "-cc-" not in usr:
                c["username"] = f"{usr}-cc-ID-s-bot01-ttl-3600"
            return c

        if "://" in s:
            u = urlparse(s)
            if not u.hostname or not u.port:
                raise ValueError("Format proxy tidak lengkap (pastikan ada host dan port).")
            scheme = (u.scheme or "http").lower()
            if scheme not in ("http", "https", "socks5"):
                scheme = "http"
            cfg: Dict[str, str] = {"server": f"{scheme}://{u.hostname}:{u.port}"}
            if u.username:
                cfg["username"] = unquote(u.username)
            if u.password is not None:
                cfg["password"] = unquote(u.password)
            return _enrich_id_proxy(cfg)

        # Format tanpa skema: host:port atau host:port:user:pass
        parts = s.split(":")
        if len(parts) == 2 and parts[1].isdigit():
            return _enrich_id_proxy({"server": f"http://{parts[0]}:{parts[1]}"})
        if len(parts) >= 4 and parts[1].isdigit():
            host, port, user = parts[0], parts[1], parts[2]
            pw = ":".join(parts[3:])
            return _enrich_id_proxy({"server": f"http://{host}:{port}", "username": user, "password": pw})

        raise ValueError(
            "Format proxy belum dikenali. Contoh yang valid:\n"
            "• host:port:username:password\n"
            "• http://username:password@host:port\n"
            "• socks5://username:password@host:port"
        )

    def _load_saved_proxy(self) -> Optional[Dict[str, str]]:
        try:
            if self.proxy_file.exists():
                data = json.loads(self.proxy_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("server"):
                    return data
        except Exception:
            pass
        env_px = os.getenv("BROWSER_PROXY_URL", "").strip()
        if env_px:
            try:
                return self.parse_proxy_input(env_px)
            except Exception:
                return None
        return None

    def proxy_summary(self) -> str:
        if not self.proxy_config or not self.proxy_config.get("server"):
            return "Mati (Pakai IP Cloud Singapore langsung)"
        srv = self.proxy_config["server"]
        has_auth = " + Login" if self.proxy_config.get("username") else ""
        return f"Aktif ({srv}{has_auth})"

    async def configure_proxy(self, raw_proxy: str) -> Dict[str, Any]:
        """Pasang atau matikan Proxy Browser secara live + langsung cek lokasi IP keluarnya."""
        clean = (raw_proxy or "").strip()
        if clean.lower() in ("off", "mati", "hapus", "none", "reset", "matikan"):
            self.proxy_config = None
            if self.proxy_file.exists():
                try:
                    self.proxy_file.unlink()
                except Exception:
                    pass
            await self._force_restart()
            ip_info = await self.check_exit_ip()
            return {"enabled": False, "ip_info": ip_info}

        parsed = self.parse_proxy_input(clean)
        self.proxy_config = parsed
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if parsed:
            self.proxy_file.write_text(json.dumps(parsed), encoding="utf-8")
        await self._force_restart()
        ip_info = await self.check_exit_ip()
        return {"enabled": bool(parsed), "server": (parsed or {}).get("server", ""), "ip_info": ip_info}

    async def check_exit_ip(self) -> str:
        """Mengecek alamat IP publik, negara, kota, dan ISP yang dipakai browser saat ini."""
        await self.ensure_alive()
        if not self.page:
            return "Browser belum aktif"
        try:
            resp = await self.page.goto("http://ip-api.com/json/?fields=status,message,country,city,isp,query", wait_until="commit", timeout=8000)
            if resp:
                txt = await asyncio.wait_for(self.page.inner_text("body"), timeout=3500)
                data = json.loads(txt.strip())
                if data.get("status") == "success":
                    return f"{data.get('query')} ({data.get('country')} · {data.get('city')} · {data.get('isp')})"
        except Exception as exc:
            return f"Gagal mengecek IP lewat proxy ({str(exc)[:70]}) — pastikan kode/kuota proxy aktif!"
        return "IP tidak terdeteksi"

    async def _idle_watchdog(self) -> None:
        import time as _t
        while self._started:
            await asyncio.sleep(30)
            if self._started and self._viewer_count <= 0 and (_t.monotonic() - self._last_active > 900):
                # Simpan tangkapan layar aktif terakhir sebelum tidur hemat RAM agar /layar tidak pernah putih kosong!
                try:
                    if self.page and not self.page.is_closed() and self.page.url != "about:blank":
                        raw = await self.page.screenshot(type="jpeg", quality=65, timeout=4000, animations="disabled")
                        b64 = base64.b64encode(raw).decode("ascii")
                        self._persist_last_screen(b64, self._display_url(self.page.url))
                except Exception:
                    pass
                try:
                    await self.stop()
                except Exception:
                    pass
                break

    async def save_state(self) -> None:
        """Simpan cookies & sesi login ke JSON super ringan (tanpa beban database Chrome penuh)."""
        if self.context:
            try:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                await asyncio.wait_for(self.context.storage_state(path=str(self.state_file)), timeout=3.0)
            except Exception:
                pass

    async def start(self, start_url: Optional[str] = None) -> None:
        self._touch()
        async with self._lock:
            if self._started and self.page and not self.page.is_closed():
                return
            await self._start_unlocked(start_url)

    async def _start_unlocked(self, start_url: Optional[str] = None) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        self._pw = await async_playwright().start()
        launch_kwargs: Dict[str, Any] = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process,TranslateUI,BlinkGenPropertyTrees,BackForwardCache",
                "--js-flags=--max-old-space-size=192",
                "--disk-cache-size=1",
                "--media-cache-size=1",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--metrics-recording-only",
                "--mute-audio",
            ],
        }
        if self.proxy_config and self.proxy_config.get("server"):
            launch_kwargs["proxy"] = dict(self.proxy_config)

        # Gunakan chromium.launch() (chromium-headless-shell ringan) bukan launch_persistent_context yang makan +120 MB RAM!
        self._browser = await self._pw.chromium.launch(**launch_kwargs)

        ctx_kwargs: Dict[str, Any] = {
            "viewport": {"width": self.VIEWPORT_WIDTH, "height": self.VIEWPORT_HEIGHT},
            "accept_downloads": True,
            "ignore_https_errors": True,
            "locale": "id-ID",
            "timezone_id": "Asia/Jakarta",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            ),
        }
        if self.state_file.exists():
            ctx_kwargs["storage_state"] = str(self.state_file)

        self.context = await self._browser.new_context(**ctx_kwargs)
        await self.context.add_init_script(
            """() => {
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                const maskCss = `
                    input[type="password"],
                    input[autocomplete="one-time-code"],
                    input[autocomplete^="cc-"],
                    input[name*="otp" i], input[id*="otp" i],
                    input[name*="pin" i], input[id*="pin" i],
                    input[name*="cvv" i], input[id*="cvv" i],
                    input[name*="api_key" i], input[name*="apikey" i],
                    input[name*="secret" i], input[name*="token" i] {
                        -webkit-text-security: disc !important;
                    }
                `;
                const injectMask = () => {
                    const root = document.head || document.documentElement;
                    if (root && !document.getElementById('__nusa_cred_mask__')) {
                        const st = document.createElement('style');
                        st.id = '__nusa_cred_mask__';
                        st.textContent = maskCss;
                        root.appendChild(st);
                    }
                };
                injectMask();
                document.addEventListener('DOMContentLoaded', injectMask);
            }"""
        )

        # Filter agresif hemat RAM: blokir video/font/iklan/websocket berat, dan blokir gambar dekoratif saat layar tidak ditonton user
        async def _block_heavy_bloat(route: Any) -> None:
            try:
                req = route.request
                rtype = req.resource_type
                rurl = req.url.lower()
                if rtype in ("media", "font", "eventsource") or any(
                    bad in rurl
                    for bad in (
                        "doubleclick.net",
                        "googlesyndication.com",
                        "adservice.google",
                        "google-analytics.com",
                        "googletagmanager.com",
                        "taboola.com",
                        "outbrain.com",
                        "criteo.com",
                        "scorecardresearch.com",
                        "amazon-adsystem.com",
                        "ads.yahoo.com",
                        "consent.yahoo.com",
                        "guce.yahoo.com",
                        "opus.analytics.yahoo.com",
                        "streamer.finance.yahoo.com",
                        "video.media.yql.yahoo.com",
                        ".mp4",
                        ".m3u8",
                        ".webm",
                    )
                ):
                    await route.abort()
                    return
                # Biarkan gambar normal tampil agar AI Vision & foto layar Telegram tetap tajam (iklan/video sudah diblokir di atas)
                await route.continue_()
            except Exception:
                pass

        try:
            await self.context.route("**/*", _block_heavy_bloat)
        except Exception:
            pass

        self.context.on("page", lambda new_page: asyncio.create_task(self._on_new_page(new_page)))
        self.page = await self.context.new_page()

        await self._attach_page(self.page)
        self._started = True
        self._touch()
        if not self._idle_task or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watchdog())

        initial_target = start_url or "about:blank"
        if initial_target != "about:blank":
            try:
                await self.navigate(initial_target)
            except Exception:
                pass

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        self._started = False
        self._screencast_on = False
        if self.context:
            try:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                await asyncio.wait_for(self.context.storage_state(path=str(self.state_file)), timeout=1.5)
            except Exception:
                pass
        try:
            if self._cdp:
                await asyncio.wait_for(self._cdp.detach(), timeout=1.0)
        except Exception:
            pass
        try:
            if self.context:
                await asyncio.wait_for(self.context.close(), timeout=2.0)
        except Exception:
            pass
        try:
            if self._browser:
                await asyncio.wait_for(self._browser.close(), timeout=2.5)
        except Exception:
            pass
        try:
            if self._pw:
                await asyncio.wait_for(self._pw.stop(), timeout=2.0)
        except Exception:
            pass
        self._cdp = None
        self.context = None
        self._browser = None
        self._pw = None
        self.page = None

    async def reset_session(self) -> None:
        """Menghapus cookies & storage browser jika user ingin logout dari semua akun."""
        if self.state_file.exists():
            try:
                self.state_file.unlink()
            except Exception:
                pass
        if not self.context:
            return
        await self.context.clear_cookies()
        if self.page:
            try:
                await self.page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
            except Exception:
                pass
            await self.navigate("about:blank")

    async def _on_new_page(self, new_page: Page) -> None:
        if self._switching_tab:
            return
        old_page = self.page
        try:
            await new_page.wait_for_load_state("commit", timeout=5000)
        except Exception:
            pass
        self.page = new_page
        await self._attach_page(new_page)
        # Tutup tab lama agar tidak pernah menumpuk >1 tab yang menggandakan pemakaian RAM!
        if old_page and old_page != new_page and not old_page.is_closed():
            try:
                await old_page.close()
            except Exception:
                pass
        await self._refresh_if_watched()

    async def _attach_page(self, page: Page) -> None:
        page.on("download", lambda dl: asyncio.create_task(self._handle_download(dl)))
        page.on("framenavigated", lambda frame: asyncio.create_task(self._on_navigated(frame)))

        if self._cdp:
            old_cdp = self._cdp
            self._cdp = None
            self._screencast_on = False

            async def _bg_detach(c: Any) -> None:
                try:
                    await asyncio.wait_for(c.detach(), timeout=1.2)
                except Exception:
                    pass

            asyncio.create_task(_bg_detach(old_cdp))

        # Hanya buka CDP session jika benar-benar ada penonton Live Web UI (_viewer_count > 0)!
        if self._viewer_count > 0:
            await self._start_screencast()

    async def _start_screencast(self) -> None:
        if self._screencast_on or not self.page:
            return
        try:
            if not self._cdp:
                self._cdp = await asyncio.wait_for(self.page.context.new_cdp_session(self.page), timeout=2.5)
                self._cdp.on(
                    "Page.screencastFrame",
                    lambda event: asyncio.create_task(self._on_screencast_frame(event)),
                )
            await asyncio.wait_for(
                self._cdp.send(
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": 50,
                        "maxWidth": self.VIEWPORT_WIDTH,
                        "maxHeight": self.VIEWPORT_HEIGHT,
                        "everyNthFrame": 6,
                    },
                ),
                timeout=2.5,
            )
            self._screencast_on = True
        except Exception:
            self._cdp = None
            self._screencast_on = False

    async def _stop_screencast(self) -> None:
        if not self._cdp or not self._screencast_on:
            return
        try:
            await self._cdp.send("Page.stopScreencast")
        except Exception:
            pass
        self._screencast_on = False

    async def set_viewer_count(self, count: int) -> None:
        """Hanya nyalakan video streaming CDP saat user membuka layar web live — hemat 70% CPU!"""
        self._viewer_count = max(0, count)
        if self._viewer_count > 0:
            self._touch()
            await self._start_screencast()
        else:
            await self._stop_screencast()

    async def _refresh_if_watched(self) -> None:
        if self._viewer_count > 0:
            await self.refresh_frame()

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
                    "title": self._cached_title,
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
            # Jika terlihat seperti domain, tambahkan https://; jika kalimat pencarian, arahkan ke Yahoo Search
            if "." in url and " " not in url:
                url = f"https://{url}"
            else:
                from urllib.parse import quote_plus
                return f"https://search.yahoo.com/search?p={quote_plus(url)}"
        # Domain lama siskopatuh.kemenag.go.id sudah resmi pindah ke siskopatuh.haji.go.id/web/
        if "siskopatuh.kemenag.go.id" in url.lower():
            return "https://siskopatuh.haji.go.id/web/"
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
            t = await asyncio.wait_for(self.page.title(), timeout=2.5)
            self._cached_title = t or ""
            return self._cached_title
        except Exception:
            return self._cached_title

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

    async def ensure_alive(self) -> None:
        """Otomatis menghidupkan ulang Chromium jika tab/proses crash atau nge-freeze >4 detik."""
        self._touch()
        needs_restart = False
        if not self._started or not self.context or not self.page:
            needs_restart = True
        else:
            try:
                if self.page.is_closed():
                    if self.context.pages:
                        self.page = self.context.pages[-1]
                        await self._attach_page(self.page)
                    else:
                        self.page = await self.context.new_page()
                        await self._attach_page(self.page)
                await asyncio.wait_for(self.page.evaluate("1"), timeout=4.5)
            except Exception:
                needs_restart = True

        if needs_restart:
            await asyncio.wait_for(self._force_restart(), timeout=25.0)

    async def _force_restart(self) -> None:
        import subprocess as _sp
        async with self._lock:
            try:
                await self._stop_unlocked()
            except Exception:
                self._started = False
            # Sapu bersih proses zombie Chromium di level OS agar RAM 512 MB tidak pernah penuh!
            try:
                _sp.run(["pkill", "-9", "-f", "headless_shell"], timeout=2, check=False)
            except Exception:
                pass
            for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                lock_file = self.profile_dir / lock_name
                if lock_file.exists() or lock_file.is_symlink():
                    try:
                        lock_file.unlink()
                    except Exception:
                        pass
            await self._start_unlocked("about:blank")

    async def _safe_close_page(self, p: Page) -> None:
        try:
            await asyncio.wait_for(p.close(), timeout=2.5)
        except Exception:
            pass

    async def refresh_frame(self, wake_if_sleeping: bool = True) -> str:
        """Mengambil screenshot terbaru secara eksplisit dan mengirimkannya ke semua klien UI."""
        if not wake_if_sleeping and (not self._started or not self.page or self.page.is_closed()):
            return self.last_real_frame_b64 or self.latest_frame_b64

        await self.ensure_alive()
        if not self.page:
            return self.last_real_frame_b64 or self.latest_frame_b64
        try:
            cur_u = self.page.url or ""
            # Jangan timpa tangkapan layar asli terakhir dengan halaman putih kosong 'about:blank'!
            if cur_u in ("", "about:blank") and self.last_real_frame_b64:
                self.latest_frame_b64 = self.last_real_frame_b64
                return self.last_real_frame_b64

            if self.show_som_badges:
                await inspect_page(self.page, show_badges=True)
            else:
                await clear_som_badges(self.page)
            raw = await self.page.screenshot(type="jpeg", quality=68, timeout=6000, animations="disabled")
            self.latest_frame_b64 = base64.b64encode(raw).decode("ascii")
            disp_u = self._display_url(cur_u)
            if cur_u and not cur_u.startswith(("about:", "chrome-error://")):
                self._persist_last_screen(self.latest_frame_b64, disp_u)
            await self.broadcast(
                {
                    "type": "frame",
                    "data": self.latest_frame_b64,
                    "url": disp_u,
                    "title": await self._safe_title(),
                }
            )
        except Exception:
            pass
        return self.latest_frame_b64 or self.last_real_frame_b64

    async def set_som_badges(self, enabled: bool) -> None:
        self.show_som_badges = enabled
        await self.refresh_frame()

    async def observe(self, annotated_screenshot: bool = True) -> Dict[str, Any]:
        """
        Membaca kondisi halaman secara menyeluruh (DOM + elemen interaktif + screenshot Vision)
        TANPA mereset/menghapus form yang sedang diisi jika skrip iklan web sempat lambat.
        """
        await self.ensure_alive()
        if not self.page:
            raise RuntimeError("Browser belum siap.")
        try:
            dom_state = await asyncio.wait_for(
                inspect_page(self.page, show_badges=(annotated_screenshot or self.show_som_badges)),
                timeout=7.5,
            )
        except Exception:
            # JANGAN langsung _force_restart() karena itu akan menghapus isi form ke about:blank!
            # Hentikan dulu loading skrip berat di halaman yang SAMA, lalu coba inspect ulang.
            page_alive = False
            try:
                if self.page and not self.page.is_closed():
                    await asyncio.wait_for(self.page.evaluate("() => { try { window.stop(); } catch(e) {} return 1; }"), timeout=2.5)
                    page_alive = True
            except Exception:
                page_alive = False

            if page_alive:
                dom_state = await asyncio.wait_for(
                    inspect_page(self.page, show_badges=False),
                    timeout=7.5,
                )
            else:
                restore_u = self.last_real_url
                await self._force_restart()
                if restore_u and not restore_u.startswith("about:"):
                    try:
                        await self.navigate(restore_u)
                    except Exception:
                        pass
                dom_state = await asyncio.wait_for(
                    inspect_page(self.page, show_badges=(annotated_screenshot or self.show_som_badges)),
                    timeout=7.5,
                )

        screenshot_b64 = ""
        try:
            raw = await self.page.screenshot(type="jpeg", quality=48, timeout=5000, animations="disabled")
            screenshot_b64 = base64.b64encode(raw).decode("ascii")
            self.latest_frame_b64 = screenshot_b64
            cur_u = self.page.url or ""
            if cur_u and not cur_u.startswith(("about:", "chrome-error://")):
                self._persist_last_screen(screenshot_b64, self._display_url(cur_u))
        except Exception:
            screenshot_b64 = self.latest_frame_b64

        if annotated_screenshot and not self.show_som_badges:
            try:
                await asyncio.wait_for(clear_som_badges(self.page), timeout=2.0)
            except Exception:
                pass

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
        """
        Mekanisme Navigasi 2-Tahap Anti-Timeout + Fresh-Tab Memory Recycle:
        - Jika pindah domain berbeda, buat tab bersih & matikan tab lama agar proses OS lama dibuang 100% (RAM turun balik ke ~310–360 MB!).
        - wait_until='commit' + batas DOM 3.8 detik + window.stop() agar tidak pernah timeout di web iklan berat.
        """
        await self.ensure_alive()
        if not self.page or not self.context:
            raise RuntimeError("Browser belum aktif.")
        target = self._normalize_url(url)

        # Fresh-Tab Swap saat pindah domain agar sampah RAM website lama langsung dihancurkan OS (tetap simpan semua cookies!)
        try:
            from urllib.parse import urlparse
            old_host = urlparse(self.page.url or "").netloc
            new_host = urlparse(target).netloc
            if old_host and new_host and old_host != new_host:
                self._switching_tab = True
                try:
                    fresh_p = await self.context.new_page()
                    old_p = self.page
                    self.page = fresh_p
                    await self._attach_page(fresh_p)
                    if not old_p.is_closed():
                        await old_p.close()
                finally:
                    self._switching_tab = False
        except Exception:
            self._switching_tab = False

        status_code = None
        try:
            resp = await self.page.goto(target, wait_until="commit", timeout=9000)
            status_code = resp.status if resp else None
        except Exception as exc:
            err_s = str(exc) or type(exc).__name__
            # JANGAN restart seluruh Chromium hanya karena 1 situs timeout / menolak koneksi cloud!
            # Cukup ganti tab bersih supaya proses Chromium tetap ringan & sehat 100%.
            if self.context:
                try:
                    self._switching_tab = True
                    hung_p = self.page
                    fresh_p = await asyncio.wait_for(self.context.new_page(), timeout=3.0)
                    self.page = fresh_p
                    await self._attach_page(fresh_p)
                    if hung_p and not hung_p.is_closed():
                        asyncio.create_task(self._safe_close_page(hung_p))
                except Exception:
                    await self._force_restart()
                finally:
                    self._switching_tab = False
            else:
                await self._force_restart()

            return (
                f"[UNREACHABLE_SITE] Gagal membuka {target}: Server website tidak merespons dalam 9 detik "
                f"atau memblokir IP server Cloud ({err_s[:80]})."
            )

        dcl_timeout = 8500 if self.proxy_config else 4800
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=dcl_timeout)
        except Exception:
            # Hentikan skrip iklan HANYA jika body halaman utama sudah muncul (jangan pernah memutus <head> SPA!)
            try:
                has_body = await asyncio.wait_for(
                    self.page.evaluate("() => Boolean(document.body && document.body.innerText && document.body.innerText.trim().length > 60)"),
                    timeout=1.5,
                )
                if has_body:
                    await asyncio.wait_for(self.page.evaluate("() => window.stop()"), timeout=1.5)
            except Exception:
                pass

        # Jika halaman berupa SPA modern (#__nuxt / #app / #root) yang sedang me-render form, beri waktu singkat sampai elemen muncul
        try:
            is_loading_spa = await asyncio.wait_for(
                self.page.evaluate(
                    "() => Boolean(document.querySelector('#__nuxt, #app, #root') && document.querySelectorAll('input, button, a[href]').length < 2)"
                ),
                timeout=1.5,
            )
            if is_loading_spa:
                for _ in range(8):
                    await asyncio.sleep(0.4)
                    cnt = await self.page.locator("input, button").count()
                    if cnt >= 2:
                        break
        except Exception:
            pass

        if self.page.url.startswith("chrome-error://"):
            try:
                await self.page.go_back(wait_until="commit", timeout=3000)
            except Exception:
                pass
            return f"Situs {target} memblokir akses dari server cloud (anti-bot/firewall)."

        asyncio.create_task(self.save_state())
        await self._refresh_if_watched()
        title = await self._safe_title()
        disp_now = self._display_url(self.page.url)
        if status_code == 403:
            return f"Membuka {disp_now} ({title}) — Catatan: Situs ini menampilkan 403/Anti-Bot terhadap IP server cloud."
        if disp_now and not disp_now.startswith(("about:", "chrome-error://")):
            self.last_real_url = disp_now
        return f"Berhasil membuka {disp_now} ({title})"

    async def wait_for_page_stable(self, timeout_ms: int = 3200) -> bool:
        """Menunggu halaman selesai memuat DOM & stabil setelah navigasi atau aksi klik."""
        if not self.page or self.page.is_closed():
            return False
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            try:
                has_body = await asyncio.wait_for(
                    self.page.evaluate("() => Boolean(document.body && document.body.innerText && document.body.innerText.trim().length > 60)"),
                    timeout=1.2,
                )
                if has_body:
                    await asyncio.wait_for(self.page.evaluate("() => window.stop()"), timeout=1.2)
            except Exception:
                pass
        await asyncio.sleep(0.15)
        return True

    async def dismiss_obstructions(self) -> None:
        """Menutup banner iklan mengambang / cookie / overlay non-modal yang menghalangi elemen target."""
        if not self.page or self.page.is_closed():
            return
        try:
            await self.page.evaluate(
                """() => {
                    for (const s of ['#fixedban', '[id*="google_ads"]', '.ad-banner', '.cookie-banner', '#onetrust-banner-sdk']) {
                        document.querySelectorAll(s).forEach(n => { n.style.display = 'none'; });
                    }
                }"""
            )
        except Exception:
            pass

    async def capture_proof_screenshot(self) -> str:
        """
        Mengambil screenshot bukti kerja yang bersih (tanpa badge SOM, dengan masking CSS input rahasia aktif).
        Mengembalikan string base64 JPEG atau '' bila halaman masih about:blank.
        """
        if not self._started or not self.page or self.page.is_closed():
            return self.last_real_frame_b64 or ""
        cur_u = self.page.url or ""
        if cur_u in ("", "about:blank") or cur_u.startswith("chrome-error://"):
            return self.last_real_frame_b64 or ""
        try:
            await clear_som_badges(self.page)
            raw = await self.page.screenshot(type="jpeg", quality=75, timeout=5000, animations="disabled")
            b64 = base64.b64encode(raw).decode("ascii")
            self.latest_frame_b64 = b64
            self._persist_last_screen(b64, self._display_url(cur_u))
            return b64
        except Exception:
            return self.latest_frame_b64 or self.last_real_frame_b64 or ""

    # ======================== PURE DOM FORM & ELEMENT HELPERS ========================

    async def _resolve_dom_locator(
        self,
        target_el: Optional[Dict[str, Any]],
        index: int,
        for_click: bool = False,
    ):
        """
        Resolusi elemen 100% berbasis DOM Selector, Label, atau Atribut HTML.
        TIDAK PERNAH menggunakan koordinat piksel (x, y).
        """
        assert self.page is not None
        if target_el:
            etype = (target_el.get("type") or "").lower()
            lbl_sel = target_el.get("label_selector") or ""
            dom_sel = target_el.get("dom_selector") or ""

            # Radio/checkbox custom dengan <input opacity:0> wajib diklik lewat <label for="...">
            if for_click and lbl_sel and etype in ("radio", "checkbox"):
                loc = self.page.locator(lbl_sel).first
                if await loc.count() > 0:
                    return loc

            if dom_sel:
                loc = self.page.locator(dom_sel).first
                if await loc.count() > 0:
                    return loc

            if lbl_sel:
                loc = self.page.locator(lbl_sel).first
                if await loc.count() > 0:
                    return loc

        if index > 0:
            by_idx = self.page.locator(f'[data-nusa-idx="{index}"]').first
            if await by_idx.count() > 0:
                if for_click and target_el and (target_el.get("type") or "").lower() in ("radio", "checkbox"):
                    proxy_loc = self.page.locator(f'[data-nusa-proxy-idx="{index}"]').first
                    if await proxy_loc.count() > 0:
                        return proxy_loc
                return by_idx

        if target_el:
            if target_el.get("label"):
                by_lbl = self.page.get_by_label(target_el["label"], exact=False).first
                if await by_lbl.count() > 0:
                    return by_lbl
            if target_el.get("placeholder"):
                by_ph = self.page.get_by_placeholder(target_el["placeholder"], exact=False).first
                if await by_ph.count() > 0:
                    return by_ph
            if target_el.get("text") and target_el.get("tag") in ("button", "a", "label", "div", "span", "li"):
                by_txt = self.page.get_by_text(target_el["text"], exact=True).first
                if await by_txt.count() > 0:
                    return by_txt

        # Bounded Fallback (1x re-inspect DOM) bila atribut data-nusa-idx sempat hilang akibat re-render SPA
        if index > 0:
            try:
                await inspect_page(self.page, show_badges=False)
                re_idx = self.page.locator(f'[data-nusa-idx="{index}"]').first
                if await re_idx.count() > 0:
                    return re_idx
            except Exception:
                pass

        return None

    async def _dom_click_locator(self, locator: Any) -> None:
        """Klik elemen murni lewat DOM (tanpa mouse.click(x,y)), otomatis singkirkan banner iklan penghalang."""
        assert self.page is not None
        try:
            await locator.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})")
        except Exception:
            pass
        try:
            await locator.click(timeout=3500)
        except Exception as exc:
            if "Target crashed" in str(exc) or "closed" in str(exc).lower():
                raise
            # Singkirkan sticky footer / fixed ad banner yang menghalangi pointer, lalu klik ulang node DOM
            try:
                await self.page.evaluate(
                    """() => {
                        for (const s of ['#fixedban', 'footer', '[id*="google_ads"]', '.ad-banner']) {
                            document.querySelectorAll(s).forEach(n => { n.style.display = 'none'; });
                        }
                    }"""
                )
                await locator.click(timeout=2500)
            except Exception:
                await locator.evaluate("el => el.click()")

    async def _fill_autocomplete_dom(
        self,
        locator: Any,
        raw_text: str,
        target_el: Optional[Dict[str, Any]],
    ) -> str:
        """
        Mengetik ke input Autocomplete / React-Select (seperti Subjects, State, City)
        dan WAJIB memilih opsi hasil autocomplete yang muncul di DOM, lalu memverifikasi hasilnya.
        """
        assert self.page is not None
        import re as _re

        fname = (target_el or {}).get("label") or (target_el or {}).get("el_id") or "Autocomplete"
        sel_info = (target_el or {}).get("dom_selector") or ""
        items = [s.strip() for s in _re.split(r"[,;]+", raw_text) if s.strip()]
        if not items:
            return f"❌ GAGAL: Nilai untuk field '{fname}' kosong."

        chosen_labels: List[str] = []
        failed_items: List[str] = []

        option_selector = (
            '.subjects-auto-complete__option, '
            '[id^="react-select-"][id*="-option-"], '
            '[class*="auto-complete__option"], '
            '[class*="__option"], '
            '[role="option"], '
            'ul[role="listbox"] li, '
            '.autocomplete-items div, '
            '.ui-menu-item'
        )

        for idx_i, item in enumerate(items):
            if idx_i == 0:
                try:
                    await locator.focus()
                except Exception:
                    await self._dom_click_locator(locator)
            else:
                await locator.focus()

            try:
                cur_raw = await locator.input_value(timeout=1500)
                if cur_raw:
                    await locator.fill("", timeout=1500)
            except Exception:
                pass
            await locator.press_sequentially(item, delay=35)

            # Tunggu daftar opsi autocomplete muncul di DOM
            opts = self.page.locator(option_selector)
            appeared = False
            for _ in range(18):
                if await opts.count() > 0:
                    appeared = True
                    break
                await asyncio.sleep(0.1)

            # Jika kata penuh (mis. 'Mathematics') tidak memunculkan opsi karena website memakai singkatan ('Maths'),
            # coba ulang dengan 4 huruf awalnya ('Math') agar opsi resmi tetap tertangkap di percobaan pertama!
            used_stem_fallback = False
            if not appeared and len(item) >= 5:
                try:
                    await locator.fill("", timeout=1500)
                    await locator.press_sequentially(item[:4], delay=35)
                    for _ in range(15):
                        if await opts.count() > 0:
                            appeared = True
                            used_stem_fallback = True
                            break
                        await asyncio.sleep(0.1)
                except Exception:
                    pass

            picked_text = ""
            if appeared:
                cnt = await opts.count()
                exact_idx = -1
                prefix_idx = -1
                contains_idx = -1
                stem_idx = -1
                item_low = item.lower()
                stem_low = item[:4].lower()

                for i in range(min(cnt, 25)):
                    cand = opts.nth(i)
                    try:
                        if not await cand.is_visible():
                            continue
                        t = (await cand.inner_text()).strip()
                    except Exception:
                        continue
                    tl = t.lower()
                    if tl == item_low and exact_idx < 0:
                        exact_idx = i
                        picked_text = t
                        break
                    elif tl.startswith(item_low) and prefix_idx < 0:
                        prefix_idx = i
                        picked_text = t
                    elif (item_low in tl or tl in item_low) and contains_idx < 0:
                        contains_idx = i
                        picked_text = t
                    elif len(stem_low) >= 4 and tl.startswith(stem_low) and stem_idx < 0:
                        stem_idx = i
                        picked_text = t

                best_idx = (
                    exact_idx if exact_idx >= 0
                    else (prefix_idx if prefix_idx >= 0 else (contains_idx if contains_idx >= 0 else stem_idx))
                )
                if best_idx >= 0:
                    chosen_opt = opts.nth(best_idx)
                    picked_text = (await chosen_opt.inner_text()).strip()
                    await self._dom_click_locator(chosen_opt)
                    await asyncio.sleep(0.12)
                    chosen_labels.append((item, picked_text, used_stem_fallback or (picked_text.lower() != item_low)))
                else:
                    # Bersihkan teks mentah yang tidak cocok agar tidak memicu salah validasi
                    try:
                        await locator.fill("", timeout=1500)
                        await self.page.keyboard.press("Escape")
                    except Exception:
                        pass
                    failed_items.append(f"'{item}' (tidak ada opsi autocomplete yang cocok)")
            else:
                try:
                    await locator.fill("", timeout=1500)
                except Exception:
                    pass
                failed_items.append(f"'{item}' (daftar saran autocomplete tidak muncul)")

        # Baca ulang chip / nilai aktual dari container DOM untuk memastikan benar-benar terpilih!
        verified_dom_val = await locator.evaluate(
            """(el) => {
                const box = el.closest('#subjectsContainer, #state, #city') || el.closest('[class$="-container"]');
                if (box) {
                    const m1 = Array.from(box.querySelectorAll('[class*="multi-value__label"]')).map(n => (n.innerText || '').trim()).filter(Boolean);
                    if (m1.length) return m1.join(', ');
                    const m2 = Array.from(box.querySelectorAll('[class*="multiValue"] > div:first-child')).map(n => (n.innerText || '').trim()).filter(Boolean);
                    if (m2.length) return m2.join(', ');
                    const single = box.querySelector('[class*="singleValue"], [class*="single-value"]');
                    if (single && (single.innerText || '').trim()) return (single.innerText || '').trim();
                }
                return (el.value || '').trim();
            }"""
        )

        if failed_items:
            return (
                f"❌ PERINGATAN AUTOCOMPLETE pada field '{fname}' ({sel_info}): "
                f"gagal memilih {', '.join(failed_items)}. Nilai tersimpan saat ini: '{verified_dom_val or '(kosong)'}'."
            )
        alias_notes = [
            f"input '{orig}' disesuaikan ke opsi resmi website '{picked}'"
            for (orig, picked, is_alias) in chosen_labels
            if is_alias
        ]
        alias_str = f" (⚠️ Catatan: {'; '.join(alias_notes)})" if alias_notes else ""
        just_picked = ", ".join(p for (_, p, _) in chosen_labels)
        return (
            f"✅ Berhasil memilih autocomplete pada field '{fname}' ({sel_info}) -> "
            f"Terpilih: '{verified_dom_val or just_picked}'{alias_str}."
        )

    async def _validate_input_dom(
        self,
        locator: Any,
        expected_text: str,
        target_el: Optional[Dict[str, Any]],
    ) -> str:
        """Memeriksa panjang karakter, atribut maxlength/minlength/pattern, format email/angka, & checkValidity() DOM."""
        import re as _re

        fname = (target_el or {}).get("label") or (target_el or {}).get("name") or (target_el or {}).get("el_id") or "Input"
        sel_info = (target_el or {}).get("dom_selector") or ""
        if await locator.count() == 0:
            return f"✅ Berhasil mengisi field '{fname}' ({sel_info})."
        meta = await locator.evaluate(
            """(el) => ({
                value: el.value !== undefined ? String(el.value) : (el.innerText || ''),
                type: (el.getAttribute('type') || '').toLowerCase(),
                id: el.id || '',
                name: el.name || '',
                maxLength: (typeof el.maxLength === 'number' && el.maxLength > 0 && el.maxLength < 524288) ? el.maxLength : null,
                minLength: (typeof el.minLength === 'number' && el.minLength > 0) ? el.minLength : null,
                pattern: el.getAttribute('pattern') || '',
                required: !!el.required,
                valid: (typeof el.checkValidity === 'function') ? el.checkValidity() : true,
                validationMsg: el.validationMessage || ''
            })"""
        )
        actual = meta.get("value", "")
        max_len = meta.get("maxLength")
        min_len = meta.get("minLength")
        pattern = meta.get("pattern") or ""
        etype = meta.get("type") or ""
        ident = f"{meta.get('id', '')} {meta.get('name', '')} {fname}".lower()

        problems: List[str] = []
        truncation_note = ""

        if max_len and len(expected_text) > max_len and actual == expected_text[:max_len]:
            truncation_note = (
                f"⚠️ BATAS KARAKTER WEBSITE pada field '{fname}': input '{expected_text}' ({len(expected_text)} karakter) "
                f"melebihi atribut maxlength={max_len} pada website sehingga terpotong menjadi '{actual}' ({len(actual)} karakter)"
            )
        elif max_len and len(expected_text) > max_len:
            problems.append(
                f"panjang input ({len(expected_text)} karakter) melebihi batas maxlength={max_len} sehingga menjadi '{actual}'"
            )
        if min_len and len(actual) < min_len:
            problems.append(f"panjang input ({len(actual)} karakter) kurang dari batas minlength={min_len}")

        # Cek khusus nomor telepon 10 digit / pattern regex HTML
        if pattern:
            try:
                if not _re.fullmatch(pattern, actual):
                    problems.append(f"nilai '{actual}' tidak sesuai format pattern HTML '{pattern}'")
            except Exception:
                pass
        elif ("usernumber" in ident or "mobile" in ident) and max_len == 10:
            if not _re.fullmatch(r"\d{10}", actual):
                problems.append(f"field '{fname}' mewajibkan tepat 10 digit angka (saat ini '{actual}', {len(actual)} karakter)")

        if etype == "email" or "email" in ident:
            if actual and not _re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", actual):
                problems.append(f"format email '{actual}' tidak valid")

        if not meta.get("valid", True) and meta.get("validationMsg"):
            problems.append(f"validasi HTML menolak nilai: {meta['validationMsg']}")

        if expected_text.strip() and not actual.strip():
            return (
                f"❌ GAGAL KETIK pada field '{fname}' ({sel_info}): kolom di layar masih KOSONG ('') setelah diketik! "
                "Pastikan klik fokus ke input yang benar atau pilih opsi yang tersedia."
            )

        if actual != expected_text and not max_len and "date" not in ident:
            if actual.strip() != expected_text.strip():
                problems.append(f"nilai aktual di DOM ('{actual}') berbeda dari yang diketik ('{expected_text}')")

        if problems:
            return (
                f"❌ MASALAH FORMAT/PANJANG pada field '{fname}' ({sel_info}): "
                + "; ".join(problems)
                + ". Wajib diperbaiki sebelum Submit!"
            )
        if truncation_note:
            return f"✅ Terisi '{actual}' pada field '{fname}' ({sel_info}) — {truncation_note}."
        return f"✅ Berhasil mengisi field '{fname}' ({sel_info}) = '{actual}' (panjang {len(actual)} karakter, format valid)."

    async def audit_form_state(self) -> Dict[str, Any]:
        """Memeriksa seluruh field di dalam <form> sebelum tombol Submit diizinkan diklik."""
        if not self.page:
            return {"has_form": False, "fields": [], "issues": []}
        return await self.page.evaluate(
            """() => {
                const form = document.querySelector('form');
                if (!form) return { has_form: false, fields: [], issues: [] };

                const fields = [];
                const issues = [];
                const radioGroups = new Map();

                const resolveLbl = (el) => {
                    if (el.id) {
                        const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                        if (l && l.innerText.trim()) return l.innerText.trim();
                    }
                    const row = el.closest('.row, .mt-2, .form-group, tr');
                    if (row) {
                        const rl = row.querySelector('label.form-label, .col-md-3 label, td:first-child, label');
                        if (rl && !rl.htmlFor && rl.innerText.trim()) return rl.innerText.trim();
                    }
                    return el.getAttribute('placeholder') || el.name || el.id || 'Field';
                };

                for (const el of Array.from(form.querySelectorAll('input:not([type="hidden"]), textarea, select'))) {
                    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
                    if (type === 'submit' || type === 'button' || type === 'file') continue;

                    const label = resolveLbl(el);
                    if (type === 'radio') {
                        const gName = el.name || el.id || 'radio';
                        if (!radioGroups.has(gName)) {
                            const row = el.closest('.row, .mt-2, .form-group') || form;
                            const gLblNode = row.querySelector('label.form-label, .col-md-3');
                            const gLabel = gLblNode ? gLblNode.innerText.trim() : gName;
                            radioGroups.set(gName, { group: gLabel, required: !!el.required, chosen: [] });
                        }
                        if (el.checked) {
                            radioGroups.get(gName).chosen.push(label || el.value);
                        }
                        if (el.required) radioGroups.get(gName).required = true;
                        continue;
                    }

                    if (type === 'checkbox') {
                        fields.push({ field: label, id: el.id, type, value: el.checked ? 'Checked' : 'Unchecked', checked: !!el.checked, valid: true });
                        continue;
                    }

                    // Cek React-Select chips / singleValue
                    let val = (el.value || '').trim();
                    const box = el.closest('#subjectsContainer, #state, #city') || el.closest('[class$="-container"]');
                    if (box && (el.id.includes('subjects') || el.id.startsWith('react-select-'))) {
                        let chips = Array.from(box.querySelectorAll('[class*="multi-value__label"]')).map(n => n.innerText.trim()).filter(Boolean);
                        if (!chips.length) {
                            chips = Array.from(box.querySelectorAll('[class*="multiValue"] > div:first-child')).map(n => n.innerText.trim()).filter(Boolean);
                        }
                        const single = box.querySelector('[class*="singleValue"], [class*="single-value"]');
                        const rawResidual = (el.value || '').trim();
                        if (rawResidual && chips.length === 0 && !(single && single.innerText.trim())) {
                            issues.push({ field: label, reason: `Teks '${rawResidual}' belum dipilih dari daftar autocomplete` });
                        }
                        val = chips.length ? chips.join(', ') : ((single && single.innerText.trim()) ? single.innerText.trim() : val);
                    }

                    const req = !!el.required || el.getAttribute('aria-required') === 'true';
                    const valid = (typeof el.checkValidity === 'function') ? el.checkValidity() : true;
                    const maxLen = (typeof el.maxLength === 'number' && el.maxLength > 0 && el.maxLength < 524288) ? el.maxLength : null;
                    const minLen = (typeof el.minLength === 'number' && el.minLength > 0) ? el.minLength : null;

                    if (req && !val) {
                        issues.push({ field: label, reason: 'Field wajib (required) masih kosong' });
                    } else if (!valid) {
                        issues.push({ field: label, reason: el.validationMessage || `Format/panjang nilai '${val}' tidak valid` });
                    } else if ((el.id === 'userNumber' || (maxLen === 10 && minLen === 10)) && val && !/^\\d{10}$/.test(val)) {
                        issues.push({ field: label, reason: `Nomor harus tepat 10 digit angka (saat ini '${val}', ${val.length} karakter)` });
                    }

                    fields.push({ field: label, id: el.id, type, value: val, required: req, valid });
                }

                for (const [, rg] of radioGroups.entries()) {
                    fields.push({ field: rg.group, type: 'radio-group', value: rg.chosen.join(', '), required: rg.required, valid: !rg.required || rg.chosen.length > 0 });
                    if (rg.required && rg.chosen.length === 0) {
                        issues.push({ field: rg.group, reason: 'Pilihan wajib (radio) belum dipilih' });
                    }
                }

                return { has_form: true, fields, issues };
            }"""
        )

    async def read_submission_result(self) -> Dict[str, Any]:
        """Membaca tabel hasil setelah klik Submit & mendeteksi jika form gagal terkirim karena error field."""
        if not self.page:
            return {"submitted": False, "modal_title": "", "rows": [], "invalid_fields": []}
        return await self.page.evaluate(
            """() => {
                const isRendered = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const st = window.getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return st.display !== 'none' && st.visibility !== 'hidden' && parseFloat(st.opacity) !== 0 && r.width > 4 && r.height > 4;
                };
                const modal = Array.from(document.querySelectorAll('.modal.show, .modal-content, [role="dialog"]')).find(isRendered) || null;
                const rows = [];
                const tables = modal
                    ? Array.from(modal.querySelectorAll('table'))
                    : Array.from(document.querySelectorAll('table:not(form table)'));

                for (const tbl of tables) {
                    if (!isRendered(tbl)) continue;
                    for (const tr of Array.from(tbl.querySelectorAll('tbody tr, tr'))) {
                        const cells = Array.from(tr.querySelectorAll('td, th')).map(c => (c.innerText || '').trim().replace(/\\s+/g, ' '));
                        if (cells.length >= 2 && cells[0] && cells[0].toLowerCase() !== 'label') {
                            rows.push({ label: cells[0], value: cells.slice(1).join(' | ') });
                        }
                    }
                }

                const invalidFields = [];
                for (const inv of Array.from(document.querySelectorAll('form :invalid, form .is-invalid'))) {
                    const tag = (inv.tagName || '').toLowerCase();
                    if (tag === 'form' || tag === 'fieldset') continue;
                    let lbl = inv.getAttribute('placeholder') || inv.name || inv.id || tag;
                    if (inv.id) {
                        const l = document.querySelector(`label[for="${CSS.escape(inv.id)}"]`);
                        if (l && l.innerText.trim()) lbl = l.innerText.trim();
                    }
                    invalidFields.push({
                        field: lbl,
                        value: inv.value || '',
                        reason: inv.validationMessage || 'Format tidak valid atau wajib diisi'
                    });
                }

                const titleNode = modal ? modal.querySelector('.modal-title, h1, h2, h3, h4') : null;
                return {
                    submitted: rows.length > 0 || !!modal,
                    modal_title: titleNode ? titleNode.innerText.trim() : '',
                    rows,
                    invalid_fields: invalidFields
                };
            }"""
        )

    async def click_element(self, index: int, elements: List[Dict[str, Any]]) -> str:
        if not self.page:
            raise RuntimeError("Browser belum aktif.")
        target_el = next((e for e in elements if e["index"] == index), None)

        if target_el and self._viewer_count > 0:
            cx = target_el["rect"]["centerX"]
            cy = target_el["rect"]["centerY"]
            lbl = target_el.get("text") or target_el.get("label") or target_el.get("placeholder") or f"#{index}"
            await self.emit_cursor(cx, cy, f"Klik [{index}] {lbl[:28]}", "click")

        locator = await self._resolve_dom_locator(target_el, index, for_click=True)
        if locator is None:
            return f"❌ Elemen [{index}] tidak ditemukan berdasarkan DOM selector maupun label."

        try:
            await self._dom_click_locator(locator)
        except Exception as exc:
            if "Target crashed" in str(exc) or "Target closed" in str(exc) or "closed" in str(exc).lower():
                await self._force_restart()
                return "Tab browser sempat penuh memori (auto-refresh bersih). Lanjutkan langkah berikutnya."
            return f"❌ Gagal klik elemen [{index}] lewat DOM selector: {exc}"

        await asyncio.sleep(0.18)
        await self._refresh_if_watched()

        # Jika elemen adalah radio/checkbox, verifikasi langsung atribut .checked pada <input> aslinya!
        if target_el and (target_el.get("type") or "").lower() in ("radio", "checkbox"):
            inp_sel = target_el.get("dom_selector") or f'[data-nusa-idx="{index}"]'
            inp_loc = self.page.locator(inp_sel).first
            if await inp_loc.count() > 0:
                try:
                    is_chk = await inp_loc.is_checked()
                    lbl_name = target_el.get("label") or target_el.get("name") or f"elemen [{index}]"
                    return f"✅ Berhasil klik '{lbl_name}' ({inp_sel}) -> Status CHECKED={is_chk}."
                except Exception:
                    pass

        desc = (target_el.get("text") or target_el.get("label") or target_el.get("dom_selector") or f"elemen [{index}]") if target_el else f"elemen [{index}]"
        return f"Berhasil klik [{index}] '{desc[:60]}'"

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

        if target_el and self._viewer_count > 0:
            cx = target_el["rect"]["centerX"]
            cy = target_el["rect"]["centerY"]
            await self.emit_cursor(cx, cy, f"Ketik '{text[:22]}'", "type")

        locator = await self._resolve_dom_locator(target_el, index, for_click=False)
        if locator is None:
            auto_inp = self.page.locator('input[type="search"], input[name*="search" i], input[name="q"], input[name="p"]').first
            if index <= 0 and await auto_inp.count() > 0:
                locator = auto_inp
            else:
                return f"❌ Field input [{index}] tidak ditemukan berdasarkan DOM selector, label, atau atribut HTML."

        # Deteksi apakah ini field Autocomplete / Subjects / React-Select!
        el_meta_id = ((target_el or {}).get("el_id") or "").lower()
        is_auto = bool((target_el or {}).get("is_autocomplete")) or ("subject" in el_meta_id) or el_meta_id.startswith("react-select-")
        if not is_auto:
            try:
                is_auto = await locator.evaluate(
                    "el => (el.getAttribute('aria-autocomplete') === 'list') || /subject|auto-complete|react-select/i.test(el.id + ' ' + el.className)"
                )
            except Exception:
                is_auto = False

        if is_auto:
            res_msg = await self._fill_autocomplete_dom(locator, text, target_el)
            await self._refresh_if_watched()
            return res_msg

        # Deteksi khusus Datepicker (contoh: #dateOfBirthInput) agar nilai lama tidak bertumpuk & popup tertutup rapi
        is_date_picker = ("date" in el_meta_id) or ("birth" in el_meta_id) or ((target_el or {}).get("type") == "date")
        await self._dom_click_locator(locator)

        if is_date_picker:
            try:
                if (target_el or {}).get("type") == "date":
                    await locator.fill(text, timeout=3000)
                else:
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.insert_text(text)
                    await self.page.keyboard.press("Enter")
                    await self.page.keyboard.press("Escape")
            except Exception:
                await locator.fill(text, timeout=3000)
        else:
            if clear_first:
                try:
                    await locator.fill(text, timeout=3500)
                except Exception:
                    await locator.press("Control+A")
                    await locator.press("Backspace")
                    await locator.press_sequentially(text, delay=18)
            else:
                await locator.press_sequentially(text, delay=18)

            # Sinkronkan state React / Vue / Angular / Formik (_valueTracker + event input & change)
            # agar nilai TIDAK PERNAH kereset jadi kosong saat pindah fokus (blur) ke kolom berikutnya!
            try:
                survived = await locator.evaluate(
                    """(el) => {
                        if (el.value !== undefined) {
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            return String(el.value || '');
                        }
                        return String(el.innerText || el.textContent || '');
                    }"""
                )
            except Exception:
                survived = ""

            # Jika teks sudah diisi via .fill() tetapi di layar/DOM masih kosong (mis. input bermasker / komponen custom SPA),
            # ketik ulang karakter-per-karakter memakai simulasi tombol fisik!
            if text and not survived.strip():
                try:
                    await self._dom_click_locator(locator)
                    await locator.press("Control+A")
                    await locator.press("Backspace")
                    await locator.press_sequentially(text, delay=22)
                    await locator.evaluate(
                        """(el) => {
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }"""
                    )
                except Exception:
                    pass

        await asyncio.sleep(0.08)
        val_report = await self._validate_input_dom(locator, text, target_el)

        if press_enter and not is_date_picker:
            try:
                await locator.press("Enter", timeout=2500)
                await asyncio.sleep(0.25)
            except Exception:
                pass

        # Selalu segarkan frame terbaru setelah mengetik supaya saat user klik Cek Layar / buka CCTV tidak pernah tampil layar lama yang masih kosong!
        try:
            raw_after = await self.page.screenshot(type="jpeg", quality=58, timeout=3500, animations="disabled")
            self.latest_frame_b64 = base64.b64encode(raw_after).decode("ascii")
            cur_u = self.page.url or ""
            if cur_u and not cur_u.startswith(("about:", "chrome-error://")):
                self._persist_last_screen(self.latest_frame_b64, self._display_url(cur_u))
            if self._viewer_count > 0:
                await self.broadcast(
                    {
                        "type": "frame",
                        "data": self.latest_frame_b64,
                        "url": self._display_url(cur_u),
                        "title": self._cached_title,
                    }
                )
        except Exception:
            await self._refresh_if_watched()
        return val_report + (" (Enter ditekan)" if press_enter else "")

    async def select_dropdown(self, index: int, option_value_or_text: str, elements: List[Dict[str, Any]]) -> str:
        if not self.page:
            raise RuntimeError("Browser belum aktif.")
        target_el = next((e for e in elements if e["index"] == index), None)
        locator = await self._resolve_dom_locator(target_el, index, for_click=False)
        if locator is None:
            return f"❌ Dropdown [{index}] tidak ditemukan berdasarkan DOM selector."

        tag_name = ((target_el or {}).get("tag") or "").lower()
        if not tag_name:
            try:
                tag_name = await locator.evaluate("el => el.tagName.toLowerCase()")
            except Exception:
                tag_name = "select"

        # Jika bukan <select> asli (mis. custom dropdown React-Select #state / #city), pilih lewat DOM option!
        if tag_name != "select":
            await self._dom_click_locator(locator)
            await asyncio.sleep(0.15)
            opts = self.page.locator('[id^="react-select-"][id*="-option-"], [class*="__option"], [role="option"], li[role="option"]')
            for _ in range(15):
                if await opts.count() > 0:
                    break
                await asyncio.sleep(0.08)
            cnt = await opts.count()
            want_low = option_value_or_text.strip().lower()
            for i in range(min(cnt, 30)):
                cand = opts.nth(i)
                try:
                    if not await cand.is_visible():
                        continue
                    t = (await cand.inner_text()).strip()
                    if t.lower() == want_low or want_low in t.lower():
                        await self._dom_click_locator(cand)
                        await self._refresh_if_watched()
                        return f"✅ Berhasil memilih opsi '{t}' pada dropdown '{(target_el or {}).get('label') or index}'."
                except Exception:
                    continue
            return f"❌ Opsi '{option_value_or_text}' tidak ditemukan pada custom dropdown [{index}]."

        try:
            await locator.select_option(label=option_value_or_text, timeout=3000)
        except Exception:
            try:
                await locator.select_option(value=option_value_or_text, timeout=3000)
            except Exception:
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
                    return f"❌ Opsi '{option_value_or_text}' tidak ditemukan pada <select> [{index}]."

        selected_txt = await locator.evaluate("el => (el.selectedOptions && el.selectedOptions[0]) ? el.selectedOptions[0].text.trim() : el.value")
        await asyncio.sleep(0.12)
        await self._refresh_if_watched()
        return f"✅ Berhasil memilih opsi '{selected_txt}' pada dropdown '{(target_el or {}).get('label') or index}'."

    async def run_page_js(self, js_code: str) -> str:
        """
        Mengeksekusi skrip JavaScript langsung di halaman aktif (untuk ekstraksi data kompleks,
        membaca atribut/Shadow DOM tersembunyi, atau interaksi widget custom yang tidak standar).
        """
        await self.ensure_alive()
        if not self.page:
            raise RuntimeError("Browser belum aktif.")
        code = (js_code or "").strip()
        if not code:
            return "❌ Kode JavaScript kosong."
        wrapped = (
            f"async () => {{ {code} }}"
            if ("return " in code or ";" in code or "\n" in code)
            else f"async () => ({code})"
        )
        try:
            res = await asyncio.wait_for(self.page.evaluate(wrapped), timeout=6.0)
            await self._refresh_if_watched()
            if isinstance(res, (dict, list)):
                out = json.dumps(res, ensure_ascii=False)
            else:
                out = "" if res is None else str(res)
            if len(out) > 4000:
                out = out[:4000] + "... [dipotong]"
            return f"✅ Hasil JS ({len(out)} karakter): {out or '(selesai tanpa return value)'}"
        except Exception as exc:
            return f"❌ Error eksekusi JS di halaman: {str(exc)[:180]}"

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
        await asyncio.sleep(0.15)
        await self._refresh_if_watched()
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

    async def get_credential_fields(self) -> List[Dict[str, Any]]:
        """Mendeteksi kolom Username/Email, Password, atau OTP di halaman agar user HP bisa isi langsung via form rapi tanpa tap-tap gambar kecil."""
        if not self.page:
            return []
        try:
            return await self.page.evaluate(
                """() => {
                    const cssEsc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/([ #;?%&,.+*~\\':"!^$[\\]()=>|/@])/g, '\\\\$1');
                    const fields = [];
                    const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="checkbox"]):not([type="radio"]):not([type="file"])'));

                    for (let i = 0; i < inputs.length; i++) {
                        const el = inputs[i];
                        if (el.disabled) continue;
                        const st = window.getComputedStyle(el);
                        if (st.display === 'none' || st.visibility === 'hidden') continue;

                        const t = (el.getAttribute('type') || 'text').toLowerCase();
                        const name = el.getAttribute('name') || '';
                        const id = el.id || '';
                        const ph = el.getAttribute('placeholder') || '';
                        const aria = el.getAttribute('aria-label') || '';

                        let lbl = '';
                        if (id) {
                            const l = document.querySelector(`label[for="${cssEsc(id)}"]`);
                            if (l) lbl = (l.innerText || '').trim();
                        }
                        if (!lbl) {
                            const wrapL = el.closest('label');
                            if (wrapL) lbl = (wrapL.innerText || '').trim();
                        }
                        if (!lbl) {
                            const prev = el.previousElementSibling;
                            if (prev && (prev.innerText || '').trim().length <= 40) lbl = (prev.innerText || '').trim();
                        }
                        if (!lbl && el.parentElement) {
                            const pPrev = el.parentElement.previousElementSibling;
                            if (pPrev && (pPrev.innerText || '').trim().length <= 40) lbl = (pPrev.innerText || '').trim();
                        }

                        const combined = `${t} ${name} ${id} ${ph} ${aria} ${lbl}`.toLowerCase();
                        const isPass = t === 'password' || /password|kata\\s*sandi|passcode/.test(combined);
                        const isOtp = /\\b(otp|2fa|kode\\s*verifikasi|verification\\s*code|pin|token)\\b/.test(combined);
                        const isUser = t === 'email' || /\\b(user|username|userid|login|email|nim|npm|nisn|nik|phone|telepon|no_hp)\\b/.test(combined);

                        if (!isPass && !isOtp && !isUser) continue;

                        let selector = '';
                        if (id) selector = `#${cssEsc(id)}`;
                        else if (name) selector = `input[name="${cssEsc(name)}"]`;
                        else if (ph) selector = `input[placeholder="${cssEsc(ph)}"]`;
                        else if (t === 'password') selector = 'input[type="password"]';
                        if (!selector) continue;

                        const cleanLbl = (lbl || ph || aria || name || (isPass ? 'Password' : (isOtp ? 'Kode OTP / PIN' : 'Username / Email'))).replace(/:$/, '').trim();
                        fields.push({
                            selector,
                            label: cleanLbl,
                            kind: isPass ? 'password' : (isOtp ? 'otp' : 'user'),
                            current_value: isPass ? '' : (el.value || ''),
                            has_existing_value: Boolean((el.value || '').trim()),
                        });
                    }
                    return fields.slice(0, 4);
                }"""
            )
        except Exception:
            return []

    async def fill_credential_fields(
        self,
        items: List[Dict[str, str]],
        click_login: bool = True,
        captcha_solver: Optional[Callable[..., Any]] = None,
        manual_captcha: str = "",
    ) -> int:
        """Mengisi kredensial langsung ke DOM Playwright + otomatis menyelesaikan CAPTCHA gambar/matematika + klik tombol Login."""
        if not self.page or not items:
            return 0
        filled = 0
        last_loc = None
        for item in items:
            sel = (item.get("selector") or "").strip()
            val = item.get("value")
            if not sel or val is None or val == "":
                continue
            try:
                loc = self.page.locator(sel).first
                await loc.scroll_into_view_if_needed(timeout=2000)
                await loc.fill(str(val), timeout=3500)
                last_loc = loc
                filled += 1
            except Exception:
                continue

        if filled > 0:
            if captcha_solver is not None or manual_captcha:
                try:
                    await self.auto_solve_captcha(solver_fn=captcha_solver, manual_answer=manual_captcha)
                except Exception:
                    pass
            if click_login:
                await asyncio.sleep(0.25)
                await self._click_login_submit_dom(last_loc)

        await asyncio.sleep(0.45)
        await self.save_state()
        await self.refresh_frame()
        return filled

    async def auto_solve_captcha(
        self,
        solver_fn: Optional[Callable[..., Any]] = None,
        manual_answer: str = "",
    ) -> str:
        """
        Universal Auto-CAPTCHA Solver (berlaku untuk SEMUA website umum, portal kampus, e-gov, maupun VueCaptcha):
        1) #VueCaptcha (canvas penjumlahan/kode + tombol pensil tersembunyi + auto-retry Refresh jika meleset)
        2) CAPTCHA Soal Matematika Teks langsung di label/halaman (contoh: 'Berapa 8 + 7 ?')
        3) CAPTCHA Gambar / Canvas umum pada form mana pun (img/canvas captcha + kolom input captcha)
        """
        if not self.page:
            return ""
        import re as _re

        def _eval_math_str(txt: str) -> str:
            m = _re.search(r"(\d{1,3})\s*([\+\-\*x×])\s*(\d{1,3})", txt or "")
            if not m:
                return ""
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            if op == "+":
                return str(a + b)
            if op == "-":
                return str(a - b)
            return str(a * b)

        # --- TIPE 1: VueCaptcha (contoh: SISKOPATUH & portal berbasis Vue) ---
        try:
            vue_canv = self.page.locator("canvas.vue-captcha-img:visible").first
            if await vue_canv.count() > 0:
                if await self.page.locator(".content-ok:visible").count() > 0:
                    return "CAPTCHA sudah terverifikasi hijau."
                for attempt in range(2):
                    ans = manual_answer.strip() if (attempt == 0 and manual_answer.strip()) else ""
                    if not ans and solver_fn is not None:
                        png_bytes = await vue_canv.screenshot(type="png", timeout=4000)
                        ans = (await solver_fn(png_bytes, "image/png", "CAPTCHA matematika atau kode")) or ""
                        ans = ans.strip()
                    if not ans:
                        break
                    pencil = self.page.locator('button[title="Written"]:visible').first
                    if await pencil.count() > 0:
                        await pencil.click(timeout=2500)
                        await asyncio.sleep(0.18)
                    cap_inp = self.page.locator('input[placeholder="insert key..."]:visible').first
                    if await cap_inp.count() > 0:
                        await cap_inp.fill(ans, timeout=2500)
                        await cap_inp.press("Enter")
                        await asyncio.sleep(0.35)
                    if await self.page.locator(".content-ok:visible").count() > 0:
                        await self._refresh_if_watched()
                        return f"CAPTCHA otomatis terisi & lolos verifikasi ('{ans}')"
                    # Jika percobaan pertama belum tembus, klik tombol Refresh CAPTCHA lalu baca gambar baru!
                    ref_btn = self.page.locator('button[title="Refresh"]:visible').first
                    if attempt == 0 and await ref_btn.count() > 0:
                        await ref_btn.click(timeout=2000)
                        await asyncio.sleep(0.4)
        except Exception:
            pass

        # --- TIPE 2 & 3: CAPTCHA Umum di Website Mana Saja (Gambar <img>/<canvas> atau Soal Hitungan Teks) ---
        try:
            cap_info = await self.page.evaluate(
                """() => {
                    const cssEsc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/([ #;?%&,.+*~\\':"!^$[\\]()=>|/@])/g, '\\\\$1');
                    const isVis = (el) => {
                        if (!el) return false;
                        const st = window.getComputedStyle(el);
                        const r = el.getBoundingClientRect();
                        return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 8 && r.height > 8;
                    };
                    const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="password"]):not([type="submit"]):not([type="button"]):not([type="checkbox"]):not([type="radio"])'));
                    let targetInp = null;
                    let nearText = '';

                    for (const inp of inputs) {
                        if (inp.disabled || !isVis(inp)) continue;
                        const meta = [
                            inp.name || '', inp.id || '', inp.placeholder || '',
                            inp.getAttribute('aria-label') || '', inp.className || ''
                        ].join(' ').toLowerCase();
                        const box = inp.closest('form, .form-group, .v-input, tr, div') || inp.parentElement;
                        const boxTxt = box ? (box.innerText || '').slice(0, 220) : '';
                        if (/captcha|securimage|security.?code|verifikasi|kode.?aman|kode.?keamanan|insert key|hitung|berapa hasil/i.test(meta + ' ' + boxTxt)) {
                            // Pastikan bukan kolom email/username utama
                            if (/username|email|userid|login|telepon|phone|search|cari/i.test(meta) && !/captcha|kode/i.test(meta)) continue;
                            targetInp = inp;
                            nearText = boxTxt;
                            break;
                        }
                    }
                    if (!targetInp) return null;

                    let inpSel = '';
                    if (targetInp.id) inpSel = `#${cssEsc(targetInp.id)}`;
                    else if (targetInp.name) inpSel = `input[name="${cssEsc(targetInp.name)}"]`;
                    else if (targetInp.placeholder) inpSel = `input[placeholder="${cssEsc(targetInp.placeholder)}"]`;
                    if (!inpSel) {
                        targetInp.setAttribute('data-auto-cap-inp', '1');
                        inpSel = 'input[data-auto-cap-inp="1"]';
                    }

                    // Cari elemen gambar / canvas CAPTCHA terdekat
                    let imgSel = '';
                    const scope = targetInp.closest('form') || document.body;
                    const mediaEls = Array.from(scope.querySelectorAll('img, canvas, svg')).filter(isVis);
                    for (const m of mediaEls) {
                        const mMeta = [m.id || '', m.className || '', m.getAttribute('src') || '', m.getAttribute('alt') || ''].join(' ').toLowerCase();
                        const r = m.getBoundingClientRect();
                        if (/captcha|securimage|verifikasi|security|kode|math/i.test(mMeta) || (m.tagName === 'CANVAS' && r.width <= 360 && r.height <= 140)) {
                            m.setAttribute('data-auto-cap-img', '1');
                            imgSel = '[data-auto-cap-img="1"]';
                            break;
                        }
                    }
                    return { inpSel, imgSel, nearText };
                }"""
            )
            if cap_info and cap_info.get("inpSel"):
                ans = manual_answer.strip()
                if not ans:
                    # Cek dulu apakah soal hitungan tertulis langsung berupa teks di sebelahnya
                    ans = _eval_math_str(cap_info.get("nearText", ""))
                if not ans and cap_info.get("imgSel") and solver_fn is not None:
                    img_loc = self.page.locator(cap_info["imgSel"]).first
                    if await img_loc.count() > 0:
                        png_bytes = await img_loc.screenshot(type="png", timeout=4000)
                        ans = (await solver_fn(png_bytes, "image/png", cap_info.get("nearText", ""))) or ""
                        ans = ans.strip()
                if ans:
                    inp_loc = self.page.locator(cap_info["inpSel"]).first
                    await inp_loc.fill(ans, timeout=3000)
                    await asyncio.sleep(0.2)
                    await self._refresh_if_watched()
                    return f"CAPTCHA otomatis terisi ('{ans}')"
        except Exception:
            pass

        return ""

    async def _click_login_submit_dom(self, fallback_loc: Any = None) -> bool:
        """Klik tombol Login / Masuk / Sign In secara otomatis lewat DOM selector setelah user mengisi kredensial."""
        if not self.page:
            return False
        login_selectors = [
            '#login-button',
            '#identifierNext button',
            '#passwordNext button',
            'button[type="submit"]:not([disabled])',
            'input[type="submit"]:not([disabled])',
            'button:has-text("LOGIN"):not([disabled])',
            'button:has-text("Login"):not([disabled])',
            'button:has-text("Log in"):not([disabled])',
            'button:has-text("Sign in"):not([disabled])',
            'button:has-text("MASUK"):not([disabled])',
            'button:has-text("Masuk"):not([disabled])',
            'button:has-text("Berikutnya"):not([disabled])',
            'button:has-text("Selanjutnya"):not([disabled])',
            'button:has-text("Lanjut"):not([disabled])',
            'button:has-text("Next"):not([disabled])',
            'button:has-text("Verify"):not([disabled])',
            'button:has-text("Verifikasi"):not([disabled])',
        ]
        for sel in login_selectors:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await self._dom_click_locator(btn)
                    return True
            except Exception:
                continue
        if fallback_loc is not None:
            try:
                await fallback_loc.press("Enter")
                return True
            except Exception:
                pass
        return False

    async def smart_login_from_chat(
        self,
        raw_text: str,
        captcha_solver: Optional[Callable[..., Any]] = None,
    ) -> str:
        """
        Memungkinkan user login cukup dengan membalas di chat Telegram (misal: `user | pass` atau `user | pass | kode_captcha`),
        Otomatis membaca & mengisi CAPTCHA gambar/matematika jika ada di halaman tanpa perlu buka layar browser!
        """
        clean = (raw_text or "").strip()
        if not clean or not self.page:
            return "Tidak ada input."

        fields = await self.get_credential_fields()
        user_f = next((f for f in fields if f["kind"] == "user"), None)
        pass_f = next((f for f in fields if f["kind"] == "password"), None)
        otp_f = next((f for f in fields if f["kind"] == "otp"), None)

        parts: List[str] = []
        if "|" in clean:
            parts = [p.strip() for p in clean.split("|")]
        elif "\n" in clean:
            lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
            if len(lines) >= 2:
                parts = lines
        elif " / " in clean:
            parts = [p.strip() for p in clean.split(" / ")]

        # Bersihkan prefix opsional seperti "user: xxx" / "pass: yyy"
        import re as _re

        def _strip_prefix(v: str) -> str:
            return _re.sub(r"^(?:username|user|email|id|password|pass|sandi|otp|kode|captcha)\s*[:=]\s*", "", v, flags=_re.I).strip()

        manual_cap = _strip_prefix(parts[2]) if len(parts) >= 3 else ""
        items_to_fill: List[Dict[str, str]] = []
        if len(parts) >= 2:
            u_val, p_val = _strip_prefix(parts[0]), _strip_prefix(parts[1])
            if p_val:
                register_runtime_secret(p_val)
            if user_f and u_val:
                items_to_fill.append({"selector": user_f["selector"], "value": u_val})
            if pass_f and p_val:
                items_to_fill.append({"selector": pass_f["selector"], "value": p_val})
            elif otp_f and p_val:
                items_to_fill.append({"selector": otp_f["selector"], "value": p_val})
        else:
            single_val = _strip_prefix(clean)
            if otp_f:
                register_runtime_secret(single_val)
                items_to_fill.append({"selector": otp_f["selector"], "value": single_val})
            elif pass_f and (not user_f or user_f.get("has_existing_value")):
                register_runtime_secret(single_val)
                items_to_fill.append({"selector": pass_f["selector"], "value": single_val})
            elif user_f and not pass_f:
                items_to_fill.append({"selector": user_f["selector"], "value": single_val})
            elif pass_f:
                register_runtime_secret(single_val)
                items_to_fill.append({"selector": pass_f["selector"], "value": single_val})

        if items_to_fill:
            filled = 0
            last_loc = None
            for item in items_to_fill:
                sel = (item.get("selector") or "").strip()
                val = item.get("value")
                if not sel or not val:
                    continue
                try:
                    loc = self.page.locator(sel).first
                    await loc.scroll_into_view_if_needed(timeout=2000)
                    await loc.fill(str(val), timeout=3500)
                    last_loc = loc
                    filled += 1
                except Exception:
                    continue

            cap_note = ""
            if captcha_solver is not None or manual_cap:
                cap_res = await self.auto_solve_captcha(solver_fn=captcha_solver, manual_answer=manual_cap)
                if cap_res:
                    cap_note = f" + {cap_res}"

            await asyncio.sleep(0.25)
            await self._click_login_submit_dom(last_loc)
            await asyncio.sleep(0.5)
            await self.save_state()
            await self.refresh_frame()
            return f"Berhasil mengisi {filled} kolom login{cap_note} & menekan tombol Masuk otomatis."
        return "Kolom login tidak ditemukan di halaman."
