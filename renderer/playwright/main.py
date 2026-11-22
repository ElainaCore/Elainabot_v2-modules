#!/usr/bin/env python
"""Playwright 浏览器渲染子引擎

按需启动浏览器, 空闲自动关闭, 通过信号量控制并发页面数, 供所有插件共享。

插件中获取 (经渲染引擎模块):
    pw = bot.module_manager.get("renderer").playwright

    # 截图 URL → bytes
    img = await pw.screenshot_url("https://example.com", full_page=True)

    # 截图 HTML 字符串 → bytes
    img = await pw.screenshot_html("<h1>Hello</h1>", viewport=(800, 600))

    # 高级: 自行操作页面
    async with pw.new_page(viewport=(1200, 800)) as page:
        await page.goto("https://example.com")
        await page.click("#btn")
        img = await page.screenshot(full_page=True)

配置文件 (renderer 模块 data/ 下自动生成):
    playwright.yaml → max_pages / headless / idle_timeout / timeout 等
"""

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager

import psutil
from core.base.logger import EXTENSION, get_logger
from modules.renderer.base import IdleEngine

log = get_logger(EXTENSION, 'Playwright')
_INSTALL_TIMEOUT = 300

_DEFAULTS = {
    'headless': True,
    'max_pages': 2,
    'idle_timeout': 300,
    'default_timeout': 30000,
    'default_viewport_width': 1280,
    'default_viewport_height': 720,
    'image_format': 'jpeg',
    'image_quality': 90,
    'browser_type': 'chromium',
    'auto_install_browser': True,
    'executable_path': '',
    'install_with_deps': False,
    'close_after_use': False,
    'launch_args': [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--disable-software-rasterizer',
        '--disable-extensions',
        '--disable-background-networking',
        # 省内存: 去掉 zygote 预热进程 + 限制 renderer 进程数 + 关闭进程级站点隔离,
        # 常驻 chromium 空闲 RSS 约 -80MB、少 2 个进程 (本地自包含 HTML 渲染无副作用)。
        '--no-zygote',
        '--renderer-process-limit=1',
        '--disable-features=site-per-process,TranslateUI',
        '--disable-accelerated-2d-canvas',
        '--disable-background-timer-throttling',
        '--mute-audio',
    ],
}

_COMMENTS = {
    'headless': '是否无头模式 (无界面)',
    'max_pages': '最大并发页面数',
    'idle_timeout': '浏览器空闲超时 (秒), 超时后自动关闭, 下次使用时重新启动',
    'default_timeout': '默认页面超时 (毫秒)',
    'default_viewport_width': '默认视口宽度',
    'default_viewport_height': '默认视口高度',
    'image_format': '截图格式: jpeg / png',
    'image_quality': '截图质量 (仅 jpeg, 1-100)',
    'browser_type': '浏览器类型: chromium / firefox / webkit',
    'auto_install_browser': '浏览器缺失时是否自动下载 Playwright 引擎',
    'executable_path': '可选浏览器可执行文件路径；留空时自动复用系统 Chromium / Chrome / Edge',
    'install_with_deps': '安装浏览器时是否同时安装系统依赖 (Docker/Linux 需要 root 和 apt)',
    'close_after_use': '用完即关: 每次调用结束后完全关闭浏览器进程, 不保留常驻进程 (适合低内存环境)',
    'launch_args': '浏览器启动参数',
}


def _system_browser_executable(browser):
    """查找可复用的系统 Chromium 浏览器，避免服务器必须下载 Playwright 浏览器。"""
    if browser != 'chromium':
        return ''
    candidates = []
    if os.name == 'nt':
        roots = [
            os.environ.get('PROGRAMFILES'),
            os.environ.get('PROGRAMFILES(X86)'),
            os.environ.get('LOCALAPPDATA'),
        ]
        for root in filter(None, roots):
            candidates.extend((
                os.path.join(root, 'Google', 'Chrome', 'Application', 'chrome.exe'),
                os.path.join(root, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
            ))
    elif sys.platform == 'darwin':
        candidates.extend((
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
        ))
    else:
        for name in (
            'chromium',
            'chromium-browser',
            'google-chrome',
            'google-chrome-stable',
            'microsoft-edge',
            'microsoft-edge-stable',
        ):
            path = shutil.which(name)
            if path:
                candidates.append(path)
    return next((path for path in candidates if os.path.isfile(path)), '')


def _resolve_browser_executable(cfg):
    """返回配置指定或系统可用的 Chromium 可执行文件。"""
    configured = str(cfg.get('executable_path') or '').strip()
    if configured:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
        if not os.path.isfile(path):
            raise ValueError(f'浏览器 executable_path 不存在: {path}')
        return path
    return _system_browser_executable(cfg.get('browser_type', 'chromium'))


# ==================== 浏览器渲染器 ====================


class PlaywrightRenderer(IdleEngine):
    """异步 Playwright 浏览器渲染器 (按需启动, 空闲关闭)"""

    __slots__ = (
        '_pw',
        '_browser',
        '_last_error',
        '_lifecycle_lock',
    )

    def __init__(self, cfg):
        browser = cfg.get('browser_type', 'chromium')
        if browser not in ('chromium', 'firefox', 'webkit'):
            raise ValueError(f'无效的 Playwright 浏览器类型: {browser!r}')
        # 用完即关模式只允许一个页面，避免并发启动多个浏览器。
        max_pages = 1 if cfg.get('close_after_use', False) else cfg.get('max_pages', 2)
        super().__init__(cfg, max_pages)
        self._pw = None
        self._browser = None
        self._last_error = None
        self._lifecycle_lock = asyncio.Lock()

    async def prepare_browser(self):
        """由渲染模块在启用时补齐浏览器，实际浏览器进程仍按需启动。"""
        executable = _resolve_browser_executable(self._cfg)
        if executable:
            log.info(f'使用系统 Chromium 浏览器: {executable}')
            return
        if not self._cfg.get('auto_install_browser', True):
            return
        browser = self._cfg.get('browser_type', 'chromium')
        with_deps = sys.platform.startswith('linux') and self._cfg.get('install_with_deps', False)
        log.info(f'正在检查并补齐 Playwright {browser} 浏览器...')
        await self._install_browser(browser, with_deps=with_deps)
        log.info(f'Playwright {browser} 浏览器已就绪')

    async def close(self):
        async with self._lifecycle_lock:
            self._closed = True
            self._stop_idle_cleanup()
            await self._close_browser()
            if self._pw:
                with contextlib.suppress(Exception):
                    await self._pw.stop()
                self._pw = None

    async def _close_browser(self):
        """关闭浏览器进程"""
        if self._browser:
            with contextlib.suppress(Exception):
                await self._browser.close()
            self._browser = None

    async def _shutdown_all(self):
        """关闭浏览器 + Playwright 进程 (用完即关模式)"""
        await self._close_browser()
        if self._pw:
            with contextlib.suppress(Exception):
                await self._pw.stop()
            self._pw = None

    async def _ensure_browser(self):
        """按需启动浏览器, 崩溃时自动重启"""
        if self._cfg.get('close_after_use', False):
            return await self._fresh_launch()
        if self._browser and self._browser.is_connected():
            return True
        async with self._lock:
            if self._browser and self._browser.is_connected():
                return True
            restarting = self._browser is not None
            log.info('浏览器已断开, 正在重启...' if restarting else '正在按需启动浏览器...')
            return await self._do_launch(restarting)

    async def _fresh_launch(self):
        """用完即关模式: 每次全新启动, 无重连检测"""
        async with self._lock:
            log.info('用完即关模式: 启动浏览器...')
            return await self._do_launch(False)

    async def _do_launch(self, restarting):
        """实际启动浏览器"""
        try:
            if restarting:
                await self._close_browser()
            if not self._pw:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
            browser_name = self._cfg.get('browser_type', 'chromium')
            launcher = getattr(self._pw, browser_name)
            self._browser = await self._launch_with_dependency_recovery(launcher, browser_name)
            self._last_error = None
            log.info('✅ 浏览器已启动' if not restarting else '✅ 浏览器已重启')
            if not self._cfg.get('close_after_use', False):
                self._start_idle_cleanup(30)
            return True
        except Exception as e:
            self._last_error = str(e)
            log.error(f'浏览器启动失败: {e}', exc_info=True)
            return False

    async def _launch_with_dependency_recovery(self, launcher, browser_name):
        """缺失浏览器文件或 Linux 动态库时，补装后重试启动。"""
        from playwright.async_api import Error

        launch_kwargs = {
            'headless': self._cfg.get('headless', True),
            'args': self._cfg.get('launch_args', []),
        }
        executable = _resolve_browser_executable(self._cfg)
        if executable:
            launch_kwargs['executable_path'] = executable
        try:
            return await launcher.launch(**launch_kwargs)
        except Error as first_error:
            if not self._cfg.get('auto_install_browser', True):
                raise
            message = str(first_error)
            missing_library = sys.platform.startswith('linux') and (
                'Host system is missing dependencies to run browsers' in message
                or 'error while loading shared libraries:' in message
                or 'cannot open shared object file' in message
            )
            if "Executable doesn't exist at" in message:
                # 启用后若缓存被清理或只有主程序缺少 headless shell，按实际 launch 结果修复。
                await self.prepare_browser()
            elif missing_library:
                log.warning(f'Playwright {browser_name} 缺少系统动态库，正在补装...')
                await self._install_browser(browser_name, with_deps=True)
            else:
                raise
            return await launcher.launch(**launch_kwargs)

    async def _install_browser(self, browser_name, *, with_deps=False):
        # Playwright CLI 会复用当前版本的缓存并补齐缺失组件，包括 Chromium headless shell。
        command = [sys.executable, '-m', 'playwright', 'install']
        if with_deps:
            command.append('--with-deps')
        command.append(browser_name)
        env = os.environ.copy()
        # 子进程也需要看到运行时注入的 .pydeps 等依赖目录。
        env['PYTHONPATH'] = os.pathsep.join(dict.fromkeys(os.path.abspath(path) for path in sys.path if path))
        # 用文件接收输出，避免 Windows 上 Node 子进程持有输出管道导致等待卡住。
        with tempfile.TemporaryFile() as output:
            process = await asyncio.create_subprocess_exec(
                *command,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                code = await asyncio.wait_for(process.wait(), _INSTALL_TIMEOUT)
                reason = f'退出码 {code}'
            except (TimeoutError, asyncio.CancelledError) as exc:
                with contextlib.suppress(psutil.Error):
                    for child in reversed(psutil.Process(process.pid).children(recursive=True)):
                        with contextlib.suppress(psutil.Error):
                            child.kill()
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                code = None
                reason = f'超过 {_INSTALL_TIMEOUT} 秒'
            if code != 0:
                size = output.seek(0, os.SEEK_END)
                output.seek(max(0, size - 4000))
                details = output.read().decode('utf-8', errors='replace').strip()
                raise RuntimeError(
                    f'Playwright {browser_name} 安装失败 ({reason})'
                    + (f': {details[-1000:]}' if details else '')
                )

    async def _release_idle(self):
        """空闲超时关闭浏览器"""
        async with self._lifecycle_lock:
            if self._active == 0 and self._browser:
                log.info(f'浏览器空闲超过 {self._cfg.get("idle_timeout", 300)}s, 自动关闭')
                await self._close_browser()

    async def _open_page(self, viewport):
        if not await self._ensure_browser():
            raise RuntimeError(f'Playwright 浏览器不可用: {self._last_error or "未知原因"}')

        vw = viewport[0] if viewport else self._cfg.get('default_viewport_width', 1280)
        vh = viewport[1] if viewport else self._cfg.get('default_viewport_height', 720)
        try:
            return await self._browser.new_page(viewport={'width': vw, 'height': vh})
        except Exception as e:
            connected = self._browser and self._browser.is_connected()
            if self._cfg.get('close_after_use', False) or ('Connection closed' not in str(e) and connected):
                raise
            log.warning(f'浏览器连接已断开, 尝试重启: {e}')
            await self._close_browser()
            if not await self._ensure_browser():
                raise RuntimeError(f'Playwright 浏览器重启失败: {self._last_error or "未知原因"}') from e
            return await self._browser.new_page(viewport={'width': vw, 'height': vh})

    # ---------- 核心 API ----------

    @asynccontextmanager
    async def new_page(self, viewport=None):
        """获取一个新页面 (async context manager), 自动限制并发

        用法:
            async with pw.new_page(viewport=(1200, 800)) as page:
                await page.goto(url)
                data = await page.screenshot()
        """
        if self._closed:
            raise RuntimeError('Playwright 已关闭')

        async with self._semaphore:
            # 先登记任务，避免启动期间被空闲回收。
            async with self._lifecycle_lock:
                if self._closed:
                    raise RuntimeError('Playwright 已关闭')
                self._active += 1
            page = None
            try:
                page = await self._open_page(viewport)
                page.set_default_timeout(self._cfg.get('default_timeout', 30000))
                yield page
            finally:
                if page is not None:
                    with contextlib.suppress(Exception):
                        await page.close()
                async with self._lifecycle_lock:
                    self._active -= 1
                    if self._active <= 0:
                        self._active = 0
                        self._mark_released()
                        if self._cfg.get('close_after_use', False):
                            await self._shutdown_all()
                        elif self._cfg.get('idle_timeout', 300) == 0:
                            await self._close_browser()

    async def screenshot_url(
        self,
        url,
        *,
        viewport=None,
        full_page=True,
        image_format=None,
        quality=None,
        wait_until='networkidle',
        wait_ms=0,
        selector=None,
        timeout=None,
    ):
        """截图指定 URL, 返回图片 bytes

        参数:
            url         — 目标 URL
            viewport    — (width, height) 元组, None 则用默认值
            full_page   — 是否全页截图
            image_format— 'jpeg' / 'png', None 则用配置默认值
            quality     — jpeg 质量 1-100, None 则用配置默认值
            wait_until  — 页面加载等待策略: 'load' / 'domcontentloaded' / 'networkidle' / 'commit'
            wait_ms     — 页面加载完成后额外等待毫秒
            selector    — CSS 选择器, 指定则只截取该元素
            timeout     — 页面 goto 超时 (毫秒), None 则用默认
        """
        fmt = image_format or self._cfg.get('image_format', 'jpeg')
        q = quality or self._cfg.get('image_quality', 90)
        to = timeout or self._cfg.get('default_timeout', 30000)

        async with self.new_page(viewport=viewport) as page:
            await page.goto(url, wait_until=wait_until, timeout=to)
            if wait_ms > 0:
                await page.wait_for_timeout(wait_ms)
            return await self._take_screenshot(page, full_page, fmt, q, selector)

    async def screenshot_html(
        self,
        html,
        *,
        viewport=None,
        full_page=True,
        image_format=None,
        quality=None,
        wait_ms=0,
        selector=None,
        base_url=None,
        wait_until='load',
    ):
        """截图 HTML 字符串, 返回图片 bytes

        参数:
            html        — HTML 内容字符串
            viewport    — (width, height) 元组
            full_page   — 是否全页截图
            image_format— 'jpeg' / 'png'
            quality     — jpeg 质量 1-100
            wait_ms     — set_content 后额外等待毫秒
            selector    — CSS 选择器, 指定则只截取该元素
            base_url    — HTML 中相对路径的基础 URL
            wait_until  — set_content 等待策略, 默认 'load' (自包含 HTML 用
                          'networkidle' 会白等 ~500ms 空闲窗口; 含外链懒加载资源
                          时才需要传 'networkidle')
        """
        fmt = image_format or self._cfg.get('image_format', 'jpeg')
        q = quality or self._cfg.get('image_quality', 90)

        async with self.new_page(viewport=viewport) as page:
            kw = {}
            if base_url:
                kw['base_url'] = base_url
            await page.set_content(html, wait_until=wait_until, **kw)
            if wait_ms > 0:
                await page.wait_for_timeout(wait_ms)
            return await self._take_screenshot(page, full_page, fmt, q, selector)

    async def screenshot_file(self, file_path, **kwargs):
        """截图本地 HTML 文件, 返回图片 bytes

        参数同 screenshot_url, file_path 为本地文件绝对路径
        """
        url = f'file:///{os.path.abspath(file_path).replace(os.sep, "/")}'
        return await self.screenshot_url(url, **kwargs)

    async def pdf_url(
        self,
        url,
        *,
        viewport=None,
        wait_until='networkidle',
        wait_ms=0,
        timeout=None,
        **pdf_kwargs,
    ):
        """将 URL 渲染为 PDF, 返回 bytes (仅 Chromium)"""
        to = timeout or self._cfg.get('default_timeout', 30000)
        async with self.new_page(viewport=viewport) as page:
            await page.goto(url, wait_until=wait_until, timeout=to)
            if wait_ms > 0:
                await page.wait_for_timeout(wait_ms)
            return await page.pdf(**pdf_kwargs)

    # ---------- 内部方法 ----------

    @staticmethod
    async def _take_screenshot(page, full_page, fmt, quality, selector):
        """统一截图逻辑"""
        kwargs = {'type': fmt, 'full_page': full_page}
        if fmt == 'jpeg':
            kwargs['quality'] = quality
        if selector:
            element = await page.query_selector(selector)
            if element:
                return await element.screenshot(**{k: v for k, v in kwargs.items() if k != 'full_page'})
        return await page.screenshot(**kwargs)
