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

import contextlib
import asyncio
import os
import sys
from contextlib import asynccontextmanager

from core.base.logger import EXTENSION, get_logger
from modules.renderer.base import IdleEngine

log = get_logger(EXTENSION, 'Playwright')

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
    'install_with_deps': '安装浏览器时是否同时安装系统依赖 (Docker/Linux 需要 root 和 apt)',
    'close_after_use': '用完即关: 每次调用结束后完全关闭浏览器进程, 不保留常驻进程 (适合低内存环境)',
    'launch_args': '浏览器启动参数',
}


# ==================== 浏览器渲染器 ====================


class PlaywrightRenderer(IdleEngine):
    """异步 Playwright 浏览器渲染器 (按需启动, 空闲关闭)"""

    __slots__ = (
        '_pw',
        '_browser',
        '_last_error',
        '_lifecycle_lock',
        '_browser_install_lock',
    )

    def __init__(self, cfg):
        # 用完即关模式只允许一个页面，避免并发启动多个浏览器。
        max_pages = 1 if cfg.get('close_after_use', False) else cfg.get('max_pages', 2)
        super().__init__(cfg, max_pages)
        self._pw = None
        self._browser = None
        self._last_error = None
        self._lifecycle_lock = asyncio.Lock()
        # Playwright 安装浏览器是进程级操作；锁可避免并发首次调用重复执行安装。
        self._browser_install_lock = asyncio.Lock()

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
            launcher = getattr(
                self._pw,
                self._cfg.get('browser_type', 'chromium'),
                self._pw.chromium,
            )
            browser_name = self._cfg.get('browser_type', 'chromium')
            await self._ensure_browser_binary(launcher, browser_name)
            self._browser = await self._launch_with_dependency_recovery(
                launcher, browser_name
            )
            log.info('✅ 浏览器已启动' if not restarting else '✅ 浏览器已重启')
            if not self._cfg.get('close_after_use', False):
                self._start_idle_cleanup(30)
            return True
        except Exception as e:
            self._last_error = str(e)
            log.error(f'浏览器启动失败: {e}', exc_info=True)
            return False

    async def _launch_with_dependency_recovery(self, launcher, browser_name):
        """启动浏览器；检测到 Linux 动态库缺失时自动补装依赖后重试一次。"""
        launch_kwargs = {
            'headless': self._cfg.get('headless', True),
            'args': self._cfg.get('launch_args', []),
        }
        try:
            return await launcher.launch(**launch_kwargs)
        except Exception as first_error:
            if not self._cfg.get('auto_install_browser', True):
                raise
            message = str(first_error)
            missing_library = (
                'error while loading shared libraries:' in message
                or 'cannot open shared object file' in message
            )
            if not missing_library:
                raise
            log.warning(
                'Chromium 已安装但缺少系统动态库，尝试执行 playwright install --with-deps'
            )
            await self._install_browser_dependencies(browser_name)
            return await launcher.launch(**launch_kwargs)

    async def _install_browser_dependencies(self, browser_name):
        """安装 Playwright 浏览器及系统依赖；失败时保留命令输出。"""
        command = [sys.executable, '-m', 'playwright', 'install', '--with-deps', browser_name]
        process = await asyncio.create_subprocess_exec(
            *command,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            details = output.decode(errors='replace').strip() if output else ''
            raise RuntimeError(
                f'Playwright {browser_name} 系统依赖安装失败 (退出码 {process.returncode})'
                + (f': {details[-1000:]}' if details else '')
            )

    async def _ensure_browser_binary(self, launcher, browser_name):
        """确保当前环境已安装 Playwright 浏览器引擎。

        Python 包安装不会自动下载浏览器；首次插件实际调用 Playwright 时，
        这里使用当前运行中的 Python 解释器执行 ``playwright install``。
        已存在的引擎直接复用，多个并发请求只会触发一次安装。
        """
        if not self._cfg.get('auto_install_browser', True):
            return

        executable = getattr(launcher, 'executable_path', None)
        if executable and os.path.isfile(executable):
            return

        async with self._browser_install_lock:
            # 等待其他协程安装完成后再次检查。
            executable = getattr(launcher, 'executable_path', None)
            if executable and os.path.isfile(executable):
                return

            log.info(f'未检测到 Playwright {browser_name} 浏览器，正在自动安装...')
            command = [sys.executable, '-m', 'playwright', 'install']
            if self._cfg.get('install_with_deps', False):
                command.append('--with-deps')
            command.append(browser_name)
            # 保留虚拟环境和 Docker 中自定义的 PLAYWRIGHT_BROWSERS_PATH 等设置。
            process = await asyncio.create_subprocess_exec(
                *command,
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
            if process.returncode != 0:
                details = output.decode(errors='replace').strip() if output else ''
                raise RuntimeError(
                    f'Playwright {browser_name} 自动安装失败 (退出码 {process.returncode})'
                    + (f': {details[-1000:]}' if details else '')
                )

            # executable_path 在不同 Playwright 版本中可能在安装后才解析；
            # 若仍不存在，让 launch() 报出原生诊断，同时记录安装已完成。
            log.info(f'✅ Playwright {browser_name} 浏览器安装完成')

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
