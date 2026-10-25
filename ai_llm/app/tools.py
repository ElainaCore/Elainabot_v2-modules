"""Private function tools, scoped to the plugin making the request.

No plugin loader, platform events, configuration store or process-wide registry.
Handlers use the native ``handler(name, arguments)`` convention; bound instance
methods and synchronous/asynchronous functions work without adapters.
"""
from __future__ import annotations

import copy
import inspect
import re
import threading
from collections.abc import Callable

_NAME = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


class PluginToolRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, dict[str, tuple[dict, Callable]]] = {}
        self._lock = threading.RLock()

    def register(self, plugin: str, tools: list[dict], handler: Callable | None) -> list[str]:
        plugin = str(plugin or '').strip()
        if not plugin:
            raise ValueError('plugin_name 不能为空')
        if not callable(handler):
            raise ValueError('注册工具需要可调用的 handler(name, arguments)')
        entries = {}
        for tool in tools or []:
            function = tool.get('function') if isinstance(tool, dict) else None
            if not isinstance(function, dict) or tool.get('type') != 'function':
                raise ValueError('工具必须是 OpenAI function 定义')
            name = function.get('name')
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ValueError('工具名称只能包含字母、数字、下划线和连字符，长度 1–64')
            schema = function.get('parameters', {'type': 'object', 'properties': {}})
            if not isinstance(schema, dict) or schema.get('type', 'object') != 'object':
                raise ValueError(f'工具 {name} 的参数必须是对象 JSON Schema')
            definition = copy.deepcopy(tool)
            definition['function'].setdefault('parameters', copy.deepcopy(schema))
            entries[name] = (definition, handler)
        # Validate the whole batch before mutating the registry.
        with self._lock:
            self._plugins.setdefault(plugin, {}).update(entries)
        return list(entries)

    def unregister(self, plugin: str) -> None:
        with self._lock:
            self._plugins.pop(str(plugin or '').strip(), None)

    def definitions(self, plugin: str) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(entry[0]) for entry in self._plugins.get(plugin, {}).values()]

    async def call(self, plugin: str, name: str, arguments: dict):
        with self._lock:
            entry = self._plugins.get(plugin, {}).get(name)
        if entry is None:
            return {'ok': False, 'error': '工具未注册或已注销'}
        value = entry[1](name, arguments)
        return await value if inspect.isawaitable(value) else value

    def snapshot(self) -> dict[str, list[str]]:
        with self._lock:
            return {plugin: list(entries) for plugin, entries in self._plugins.items()}

    def clear(self) -> None:
        with self._lock:
            self._plugins.clear()
