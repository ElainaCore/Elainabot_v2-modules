# Renderer 模块接入文档

Renderer 为所有插件共享两种渲染引擎：PIL 子进程渲染池和 Playwright 浏览器渲染器。模块按需创建资源，并在空闲或卸载时回收。

## 获取模块

```python
from core.application import get_app

app = get_app()
renderer = app.module_manager.get("renderer") if app else None
if renderer and renderer.playwright_available():
    image_bytes = await renderer.playwright.screenshot_html("<h1>Hello</h1>")
```

模块未启用、依赖未安装或对应引擎关闭时，`get()` 返回 `None` 或子属性返回 `None`。用 `pil_available()` / `playwright_available()` 判断，不要直接访问私有字段。

配置文件位于 `modules/renderer/data/`：

| 文件 | 关键配置 |
| --- | --- |
| `config.yaml` | `pil_enabled`、`playwright_enabled` |
| `pil.yaml` | worker 数量、并发数、超时和空闲回收 |
| `playwright.yaml` | 浏览器类型、页面并发、视口、截图格式和超时 |

## PIL 子进程渲染

插件统一使用 Renderer 模块公开的专用协议，不直接获取进程池。

```python
from PIL import Image, ImageDraw
from io import BytesIO
from modules.renderer.protocol import render_pil

def render_card(title: str):
    image = Image.new("RGB", (800, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.text((32, 32), title, fill="black")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), image.width, image.height

image_bytes, width, height = await render_pil(
    "plugins.example.render:render_card",
    "来自插件的卡片",
)
```

`await render_pil(target, *args, **kwargs)` 只发送目标 ID 和数据，不发送 Python callable。`target` 格式为 `plugins.<插件>.<模块>:<模块级函数>`；参数和返回值需可被 `pickle` 序列化。进程池固定使用 `spawn` 干净启动，因此 worker 不会继承主框架及全部已加载插件的内存，也不会额外保留 forkserver 进程。进程按并发需求创建；worker 周期性释放 Python 和 glibc 空闲堆，整池达到任务阈值后在并发归零的间隙重建，空闲超过 `idle_timeout` 后退出。任务超时或 worker 崩溃时会强制回收并重建进程池。

## Playwright 浏览器渲染

通过 `renderer.playwright` 获取 `PlaywrightRenderer`。首次使用时启动浏览器。

### 快捷截图 API

```python
pw = renderer.playwright
if pw:
    by_url = await pw.screenshot_url(
        "https://example.com",
        viewport=(1200, 800),
        full_page=True,
        image_format="png",
        wait_until="networkidle",
    )
    by_html = await pw.screenshot_html(
        "<main><h1>报告</h1></main>",
        viewport=(900, 600),
        image_format="jpeg",
        quality=85,
    )
```

| 方法 | 返回值 | 主要参数 |
| --- | --- | --- |
| `await screenshot_url(url, *, viewport=None, full_page=True, image_format=None, quality=None, wait_until='networkidle', wait_ms=0, selector=None, timeout=None)` | `bytes` | 截图 URL；`selector` 只截取匹配元素 |
| `await screenshot_html(html, *, viewport=None, full_page=True, image_format=None, quality=None, wait_ms=0, selector=None, base_url=None, wait_until='load')` | `bytes` | 截图 HTML 字符串 |
| `await screenshot_file(file_path, **kwargs)` | `bytes` | 截图本地 HTML 文件，路径应为绝对路径 |
| `await pdf_url(url, *, viewport=None, wait_until='networkidle', wait_ms=0, timeout=None, **pdf_kwargs)` | `bytes` | Chromium 将 URL 导出为 PDF |

`image_format` 支持 `jpeg` 和 `png`；JPEG 的 `quality` 范围为 1-100。返回值是原始 bytes，可直接交给 `event.reply_image()`、图床模块或写入文件。

### 高级页面操作

```python
async with pw.new_page(viewport=(1200, 800)) as page:
    await page.goto("https://example.com", wait_until="domcontentloaded")
    await page.click("button#load")
    await page.wait_for_timeout(300)
    image_bytes = await page.screenshot(full_page=True, type="png")
```

`new_page()` 是异步上下文管理器，会限制并发、自动关闭页面并处理浏览器重启。不要把 `page` 对象带出 `async with` 作用域。

## 资源与性能

- 渲染 API 都是异步的；不要在事件循环中自行调用大型同步 PIL 绘制。
- 浏览器页面并发受 `max_pages` 限制，超出会排队。
- `close_after_use: true` 适合低内存环境，但每次调用都会重新启动浏览器，并自动将页面并发限制为 1，避免并发请求互相关闭或覆盖 browser。
- 插件卸载时无需调用 `close()`；由 Renderer 模块统一回收。
