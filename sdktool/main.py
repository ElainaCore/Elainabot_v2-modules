"""SDK 工具模块入口。"""

__module_meta__ = {
    'name': 'SDK工具',
    'description': '以模块方式兼容多种 SDK 插件',
    'version': '0.2.0',
    'author': 'ElainaBot',
}

from .runtime import SdkToolRuntime

_instance = None


async def setup(ctx):
    """启动 SDK 工具运行时。"""
    global _instance
    _instance = SdkToolRuntime(ctx)
    await _instance.start()
    return _instance


async def teardown():
    """停止 SDK 工具运行时。"""
    global _instance
    if _instance is not None:
        await _instance.stop()
    _instance = None
