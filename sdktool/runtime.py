"""SDK 工具运行时。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import uuid
from collections.abc import Callable
from inspect import isawaitable
from pathlib import Path
from typing import Any

from core.application import get_app
from core.base.config import cfg
from core.base.logger import EXTENSION, get_logger
from core.plugin.context import PluginContext, PluginInfo

from .adapters import AdapterRegistry, NodeAdapter

log = get_logger(EXTENSION, 'SDK工具')

_SENDER_METHODS = frozenset(
    {
        'reply', 'reply_stream', 'send_stream_to_user', 'reply_image',
        'reply_voice', 'reply_video', 'reply_file', 'reply_ark',
        'reply_card', 'send_to_group', 'send_to_user', 'send_to_channel',
        'send_image', 'send_wakeup', 'force_wakeup', 'ack_interaction',
        'recall', 'get_share_link', 'get_global_menu', 'update_global_menu',
        'get_panels', 'create_panel', 'get_panel', 'update_panel',
        'delete_panel', 'update_panel_targets', 'get_group_member',
        'get_group_record', 'get_group_info', 'get_group_bot_state',
        'refresh_group_info', 'get_group_join_requests',
        'review_group_join_request', 'get_group_restrict_chat_setting',
        'set_group_member_mute', 'get_bot_member', 'get_image_size',
        'upload_media',
    }
)
_EVENT_ARGUMENT_METHODS = frozenset(
    {
        'reply', 'reply_stream', 'reply_image', 'reply_voice', 'reply_video',
        'reply_file', 'reply_ark', 'reply_card', 'ack_interaction', 'recall',
        'upload_media',
    }
)
_CONTEXT_METHODS = frozenset(
    {
        'get_data_path', 'get_resource_path', 'read_config', 'save_config',
        'ensure_config', 'read_data', 'save_data', 'data_exists', 'list_data',
        'read_data_async', 'save_data_async', 'read_config_async',
        'save_config_async',
    }
)
_CONFIG_METHODS = frozenset(
    {'get', 'get_bot_configs', 'get_bot_config', 'get_bot_setting', 'set_bot_setting'}
)
_LOG_METHODS = frozenset({'debug', 'info', 'warning', 'error', 'exception'})
_IMAGE_HOSTING_METHOD = re.compile(
    r'^(?:status|upload_any|is_[a-z0-9_]+_available|upload_[a-z0-9_]+(?:_url)?|list_[a-z0-9_]+_assets|delete_[a-z0-9_]+)$'
)


def _json_safe(value):
    """转换值为可传输的 JSON 数据。"""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return {'__base64__': base64.b64encode(value).decode('ascii')}
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _decode_json_value(value):
    """还原 SDK 二进制值并递归转换参数。"""
    if isinstance(value, dict):
        if value.get('__base64__') is not None:
            try:
                return base64.b64decode(str(value['__base64__']))
            except Exception:
                return value
        if value.get('type') == 'Buffer' and isinstance(value.get('data'), list):
            try:
                return bytes(int(x) & 0xFF for x in value['data'])
            except (TypeError, ValueError):
                return value
        return {str(k): _decode_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_json_value(v) for v in value]
    return value


def _snake_case(value):
    value = re.sub(r'(?<!^)(?=[A-Z])', '_', str(value)).lower()
    return value.replace('-', '_')


def _rpc_arguments(params):
    raw_args = _decode_json_value(params.get('args') or [])
    args = raw_args if isinstance(raw_args, list) else []
    raw_options = _decode_json_value(params.get('options') or {})
    options = {
        _snake_case(key): value
        for key, value in raw_options.items()
    } if isinstance(raw_options, dict) else {}
    return args, options


async def _call_rpc(func, args, options):
    result = func(*args, **options)
    return await result if isawaitable(result) else result


def _event_payload(event):
    fields = (
        'appid', 'event_id', 'event_type', 'message_id', 'message_type',
        'content', 'raw_content', 'timestamp', 'user_id', 'raw_user_id',
        'username', 'member_role', 'union_openid', 'is_bot', 'group_id',
        'guild_id', 'channel_id', 'message_scene', 'parallel_message',
        'message_reference_id', 'msg_elements', 'attachments', 'image_url',
        'is_group', 'is_direct', 'is_channel', 'is_interaction',
        'is_lifecycle', 'interaction_data', 'scene', 'sharer_id',
        'subscribe_results', 'mentions', 'bot_member_role', 'join_request_id',
        'apply_at', 'apply_source', 'invited_by', 'verify_info',
        'verify_method', 'review_qa_list', 'is_at_self', 'is_at_other_bot',
        'is_at_other_user', 'is_at_all', 'callback_code', 'error',
    )
    payload = {name: _json_safe(getattr(event, name, None)) for name in fields}
    payload['chat_type'] = getattr(event, 'chat_type', '')
    payload['chat_id'] = getattr(event, 'chat_id', '')
    payload['raw'] = _json_safe(getattr(event, 'raw', None))
    return payload


def _match_payload(match):
    groups = list(match.groups())
    return {
        '0': match.group(0),
        'captures': [_json_safe(x) for x in groups],
        'groups': [_json_safe(x) for x in groups],
        'named': _json_safe(match.groupdict()),
        'index': match.start(),
        'input': match.string,
        'end': match.end(),
    }


class _JsonProcess:
    """管理一个 SDK 子进程及其请求状态。"""

    def __init__(self, plugin_dir: Path, command: list[str], launch_env: dict[str, str], on_request: Callable, *, plugin_name='', plugin_ctx=None):
        self.plugin_dir = plugin_dir
        self.command = command
        self.launch_env = launch_env
        self.on_request = on_request
        self.plugin_name = plugin_name or plugin_dir.name
        self.plugin_ctx = plugin_ctx
        self.process = None
        self._reader_task = None
        self._stderr_task = None
        self._pending: dict[str, asyncio.Future] = {}
        self._active_events: dict[str, Any] = {}
        self._write_lock = asyncio.Lock()
        self._ready: asyncio.Future | None = None
        self.handlers: list[dict[str, Any]] = []
        self.interceptors: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {}
        self.lifecycle: dict[str, Any] = {}
        self._stopping = False

    async def start(self):
        env = os.environ.copy()
        env['ELAINA_PLUGIN_NAME'] = self.plugin_name
        env['ELAINA_PLUGIN_DIR'] = str(self.plugin_dir)
        env.update(self.launch_env)
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=str(self.plugin_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        try:
            await asyncio.wait_for(self._ready, timeout=15)
        except Exception:
            await self.stop()
            raise
        if not self.handlers and not self.interceptors and not any(self.lifecycle.values()):
            await self.stop()
            raise RuntimeError('JS 插件未注册 handler、interceptor 或生命周期钩子')

    async def _read_stdout(self):
        assert self.process is not None and self.process.stdout is not None
        try:
            async for line in self.process.stdout:
                try:
                    msg = json.loads(line.decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    log.warning('[%s] Node stdout: %s', self.plugin_dir.name, line.decode('utf-8', 'replace').rstrip())
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get('type')
                if kind == 'ready':
                    self.handlers = msg.get('handlers') if isinstance(msg.get('handlers'), list) else []
                    self.interceptors = msg.get('interceptors') if isinstance(msg.get('interceptors'), list) else []
                    self.meta = msg.get('meta') if isinstance(msg.get('meta'), dict) else {}
                    self.lifecycle = msg.get('lifecycle') if isinstance(msg.get('lifecycle'), dict) else {}
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_result(True)
                elif kind in ('response', 'event_result'):
                    self._resolve(msg)
                elif kind == 'request':
                    asyncio.create_task(self._handle_request(msg))
                elif kind == 'log':
                    log.info('[%s] %s', self.plugin_dir.name, str(msg.get('message', '')))
        except asyncio.CancelledError:
            raise
        finally:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(RuntimeError('Node 进程提前退出'))
            error = RuntimeError(f'JS 插件进程已退出: {self.plugin_dir.name}')
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()

    async def _read_stderr(self):
        assert self.process is not None and self.process.stderr is not None
        async for line in self.process.stderr:
            log.info('[%s] %s', self.plugin_dir.name, line.decode('utf-8', 'replace').rstrip())

    async def _handle_request(self, msg):
        request_id = str(msg.get('id') or '')
        try:
            result = await self.on_request(msg.get('method', ''), msg.get('params') or {})
            await self.send({'type': 'response', 'id': request_id, 'ok': True, 'result': _json_safe(result)})
        except Exception as error:
            await self.send({'type': 'response', 'id': request_id, 'ok': False, 'error': str(error)})

    def _resolve(self, msg):
        future = self._pending.pop(str(msg.get('id') or ''), None)
        if future is None or future.done():
            return
        if msg.get('ok', True):
            future.set_result(msg.get('result'))
        else:
            future.set_exception(RuntimeError(str(msg.get('error') or 'JS 请求失败')))

    async def send(self, message):
        if self.process is None or self.process.stdin is None or self.process.returncode is not None:
            raise RuntimeError('JS 插件进程未运行')
        async with self._write_lock:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
            await self.process.stdin.drain()

    async def request(self, method, params, *, timeout=30):
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self.send({'type': 'request', 'id': request_id, 'method': method, 'params': params})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def invoke(self, handler_id, event, match):
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._active_events[request_id] = event
        await self.send(
            {
                'type': 'event',
                'id': request_id,
                'handler': handler_id,
                'event': _event_payload(event),
                'match': _match_payload(match),
            }
        )
        try:
            return await asyncio.wait_for(future, timeout=300)
        finally:
            self._pending.pop(request_id, None)
            self._active_events.pop(request_id, None)

    async def invoke_interceptor(self, interceptor_id, event):
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._active_events[request_id] = event
        await self.send(
            {
                'type': 'interceptor',
                'id': request_id,
                'interceptor': interceptor_id,
                'event': _event_payload(event),
            }
        )
        try:
            return await asyncio.wait_for(future, timeout=300)
        finally:
            self._pending.pop(request_id, None)
            self._active_events.pop(request_id, None)

    async def stop(self):
        if self._stopping:
            return
        self._stopping = True
        if self.process is None:
            return
        with contextlib.suppress(Exception):
            await self.send({'type': 'shutdown'})
        try:
            await asyncio.wait_for(self.process.wait(), timeout=3)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        self.process = None


class SdkToolRuntime:
    def __init__(self, ctx):
        self.ctx = ctx
        self.plugins_dir = Path(ctx._root_dir).parent.parent / 'plugins'
        self.processes: dict[str, _JsonProcess] = {}
        self.plugin_dirs: dict[str, Path] = {}
        self.plugin_adapters = {}
        self.plugin_manager = None
        self.adapters = AdapterRegistry([NodeAdapter(Path(__file__).parent)])
        self._watch_task = None
        self._attach_task = None
        self._mtimes: dict[str, int] = {}

    async def start(self):
        self._attach_task = asyncio.create_task(self._wait_for_plugin_manager())

    async def _wait_for_plugin_manager(self):
        for _ in range(100):
            app = get_app()
            manager = app.plugin_manager if app is not None else None
            if manager is not None:
                await self.attach(manager)
                return
            await asyncio.sleep(0.1)

    async def attach(self, plugin_manager):
        self.plugin_manager = plugin_manager
        if not self.plugins_dir.is_dir():
            return
        for plugin_dir in sorted(self.plugins_dir.iterdir()):
            if plugin_dir.is_dir() and not plugin_dir.name.startswith(('_', '.')):
                await self._load_plugin(plugin_dir)
        self._watch_task = asyncio.create_task(self._watch_loop())

    async def _load_plugin(self, plugin_dir: Path):
        manifest_path = plugin_dir / 'elaina-plugin.json'
        if not manifest_path.is_file():
            return
        process = None
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            adapter = self.adapters.resolve(manifest)
            if adapter is None:
                return
            if not adapter.available():
                log.warning('SDK 运行环境不可用: %s', adapter.name)
                return
            entry_path = (plugin_dir / adapter.entry(manifest)).resolve()
            if not entry_path.is_file() or not entry_path.is_relative_to(plugin_dir.resolve()):
                raise ValueError('entry 不在插件目录内或文件不存在')
            name = str(manifest.get('name') or plugin_dir.name)
            if self.plugin_manager.is_plugin_disabled(name) or self.plugin_manager.is_plugin_disabled(plugin_dir.name):
                log.info('JS 插件 [%s] 已禁用, 跳过启动', name)
                return
            launch = adapter.build(manifest, plugin_dir, entry_path)
            plugin_ctx = PluginContext(name, str(plugin_dir))
            process = _JsonProcess(
                plugin_dir,
                list(launch.command),
                launch.env,
                lambda method, params: self._handle_api(process, method, params),
                plugin_name=name,
                plugin_ctx=plugin_ctx,
            )
            await process.start()
            if name in self.plugin_manager.plugins:
                raise ValueError(f'插件名冲突: {name}')
            meta = dict(process.meta)
            meta.update(manifest.get('meta') or {})
            for key in ('name', 'version', 'description', 'author', 'github', 'homepage', 'license'):
                if key in manifest and key not in meta:
                    meta[key] = manifest[key]
            plugin_info = self._register_plugin(name, plugin_dir, process, plugin_ctx, meta)
            plugin_info.ctx = plugin_ctx
            self.processes[name] = process
            self.plugin_dirs[name] = plugin_dir
            self.plugin_adapters[name] = adapter
            self._snapshot_plugin(name, plugin_dir)
            log.info(
                'SDK 插件已加载: %s (%d handlers, %d interceptors)',
                name,
                len(process.handlers),
                len(process.interceptors),
            )
        except Exception as error:
            if process is not None and process not in self.processes.values():
                with contextlib.suppress(Exception):
                    await process.stop()
            log.error('JS 插件加载失败 [%s]: %s', plugin_dir.name, error)

    async def _reload_plugin(self, name):
        process = self.processes.pop(name, None)
        plugin_dir = self.plugin_dirs.get(name)
        await self._unregister_plugin(name)
        if process:
            with contextlib.suppress(Exception):
                await process.stop()
        if plugin_dir is None:
            # 清单名称可能与目录名称不同。
            for candidate in self.plugins_dir.iterdir():
                if (candidate / 'elaina-plugin.json').is_file():
                    try:
                        if json.loads((candidate / 'elaina-plugin.json').read_text(encoding='utf-8')).get('name') == name:
                            plugin_dir = candidate
                            break
                    except Exception:
                        continue
        if plugin_dir:
            await self._load_plugin(plugin_dir)
        return True

    async def _unregister_plugin(self, name):
        if self.plugin_manager is None:
            return
        plugin = self.plugin_manager._plugins.pop(name, None)
        self.plugin_dirs.pop(name, None)
        self.plugin_adapters.pop(name, None)
        if plugin is not None:
            for callback, is_coro in plugin.on_unload_funcs:
                with contextlib.suppress(Exception):
                    result = callback()
                    if is_coro or isawaitable(result):
                        await result
            self.plugin_manager._rebuild_handler_list()

    def _register_plugin(self, name, plugin_dir, process, plugin_ctx, meta):
        plugin = PluginInfo(name, str(plugin_dir))
        plugin.ctx = plugin_ctx
        plugin.is_large = True
        plugin.meta = {str(key): str(value) for key, value in meta.items() if value is not None}
        for index, raw in enumerate(process.handlers):
            handler_id = str(raw.get('id') or raw.get('name') or f'handler-{index}')
            pattern = str(raw.get('pattern') or '')
            if not pattern:
                raise ValueError(f'处理器缺少 pattern: {handler_id}')
            raw_types = raw.get('event_types')
            event_types = ({raw_types} if isinstance(raw_types, str) else raw_types) or ()
            handler = {
                'func': lambda event, match, item=handler_id: process.invoke(item, event, match),
                'is_coro': True,
                'pattern': pattern,
                'compiled': re.compile(pattern, re.DOTALL),
                'name': str(raw.get('name') or handler_id),
                'desc': str(raw.get('desc') or raw.get('description') or ''),
                'priority': int(raw.get('priority') or 0),
                'owner_only': bool(raw.get('owner_only', False)),
                'group_only': bool(raw.get('group_only', False)),
                'direct_only': bool(raw.get('direct_only', False)),
                'channel_only': bool(raw.get('channel_only', False)),
                'event_types': frozenset(event_types) or None,
                'cooldown': raw.get('cooldown', 0),
                'ignore_at_check': bool(raw.get('ignore_at_check', False)),
                'block': bool(raw.get('block', False)),
                'fallback': bool(raw.get('fallback', False)),
                '_file': handler_id,
            }
            plugin.handlers.append(handler)
        for index, raw in enumerate(process.interceptors):
            interceptor_id = str(raw.get('id') or f'interceptor-{index}')
            plugin.interceptors.append({
                'func': lambda event, item=interceptor_id: process.invoke_interceptor(item, event),
                'is_coro': True,
                'priority': int(raw.get('priority', 100)),
                '_file': interceptor_id,
            })
        plugin.on_unload_funcs = [(process.stop, True)]
        self.plugin_manager._plugins[name] = plugin
        self.plugin_manager._rebuild_handler_list()
        return plugin

    def _snapshot_plugin(self, name, plugin_dir):
        prefix = str(plugin_dir)
        for key in [item for item in self._mtimes if item.startswith(prefix)]:
            self._mtimes.pop(key, None)
        for path in plugin_dir.rglob('*'):
            if self._is_source_file(path):
                with contextlib.suppress(OSError):
                    self._mtimes[str(path)] = path.stat().st_mtime_ns

    def _is_source_file(self, path):
        return path.is_file() and (path.name == 'elaina-plugin.json' or path.suffix in self.adapters.source_suffixes())

    async def _watch_loop(self):
        while True:
            await asyncio.sleep(2)
            for name, plugin_dir in list(self.plugin_dirs.items()):
                changed = False
                current = {}
                for path in plugin_dir.rglob('*'):
                    if self._is_source_file(path):
                        with contextlib.suppress(OSError):
                            current[str(path)] = path.stat().st_mtime_ns
                keys = {key for key in self._mtimes if key.startswith(str(plugin_dir))}
                changed = any(self._mtimes.get(key) != value for key, value in current.items()) or keys != set(current)
                if changed:
                    await self._reload_plugin(name)

    async def _handle_api(self, process, method, params):
        if not isinstance(params, dict):
            raise ValueError('RPC params 必须是对象')
        method = str(method or '')
        event_id = str(params.get('event_id') or '')
        event = process._active_events.get(event_id)
        legacy_args = _decode_json_value(params.get('args') or {})

        if method.startswith('context.'):
            name = method.removeprefix('context.')
            if name not in _CONTEXT_METHODS or process.plugin_ctx is None:
                raise ValueError(f'不支持的 PluginContext API: {name}')
            args, options = _rpc_arguments(params)
            return await _call_rpc(getattr(process.plugin_ctx, name), args, options)

        if method.startswith('log.'):
            name = method.removeprefix('log.')
            if name not in _LOG_METHODS or process.plugin_ctx is None:
                raise ValueError(f'不支持的日志 API: {name}')
            raw = _decode_json_value(params.get('args') or [])
            values = raw if isinstance(raw, list) else [raw]
            if values:
                getattr(process.plugin_ctx.log, name)(*values)
            return True

        if method.startswith('config.'):
            name = method.removeprefix('config.')
            if name not in _CONFIG_METHODS:
                raise ValueError(f'不支持的配置 API: {name}')
            args, options = _rpc_arguments(params)
            if options:
                raise ValueError('配置 API 不接受 options')
            return await _call_rpc(getattr(cfg, name), args, {})

        if method == 'module.call':
            return await self._call_module(process, params)

        if method.startswith('event.') and event is None:
            raise ValueError('事件已结束或 event_id 无效')

        # 兼容旧版协议的对象参数格式。
        if method == 'event.reply' and isinstance(legacy_args, dict):
            options = legacy_args.get('options') or {}
            options = {_snake_case(k): v for k, v in options.items()}
            return await event.sender.reply(event, legacy_args.get('content'), **options)
        if method == 'event.reply_card' and isinstance(legacy_args, dict):
            options = legacy_args.get('options') or {}
            auto_delete_time = options.get('autoDeleteTime', options.get('auto_delete_time'))
            return await event.sender.reply_card(
                event,
                legacy_args.get('card_type', 'tuwen'),
                legacy_args.get('data'),
                legacy_args.get('content', ''),
                auto_delete_time=auto_delete_time,
            )
        if method == 'event.recall' and isinstance(legacy_args, dict):
            return await event.sender.recall(event, legacy_args.get('message_id'))
        if method == 'event.set_callback_code' and isinstance(legacy_args, dict):
            event.set_callback_code(legacy_args.get('code', 0))
            return True
        if method == 'event.set_ack_timeout' and isinstance(legacy_args, dict):
            event.set_ack_timeout(legacy_args.get('seconds', 10))
            return True
        if method == 'event.get' and isinstance(legacy_args, dict):
            return event.get(legacy_args.get('path', ''))
        if method == 'event.send_to_group' and isinstance(legacy_args, dict):
            options = {_snake_case(k): v for k, v in (legacy_args.get('options') or {}).items()}
            return await event.sender.send_to_group(
                legacy_args.get('group_id'), legacy_args.get('content'), **options
            )
        if method == 'event.send_to_user' and isinstance(legacy_args, dict):
            options = {_snake_case(k): v for k, v in (legacy_args.get('options') or {}).items()}
            return await event.sender.send_to_user(
                legacy_args.get('user_id'), legacy_args.get('content'), **options
            )

        prefix, separator, name = method.partition('.')
        if not separator or prefix not in {'event', 'sender', 'bot'} or name not in _SENDER_METHODS:
            raise ValueError(f'不支持的 JS API: {method}')
        args, options = _rpc_arguments(params)
        if prefix == 'event':
            sender = event.sender
            if name in _EVENT_ARGUMENT_METHODS:
                args.insert(0, event)
        else:
            appid = str(params.get('appid') or '')
            if not appid:
                raise ValueError('主动调用 Sender API 必须使用 plugin.bot(appid)')
            app = get_app()
            bot = app.get_bot(appid) if app is not None else None
            if bot is None:
                raise ValueError(f'机器人未运行或 appid 无效: {appid}')
            sender = bot.sender
        return await _call_rpc(getattr(sender, name), args, options)

    async def _call_module(self, process, params):
        module_name = str(params.get('name') or '')
        method = _snake_case(params.get('method') or '')
        if module_name != 'image_hosting' or not _IMAGE_HOSTING_METHOD.fullmatch(method):
            raise ValueError(f'不支持的模块 API: {module_name}.{method}')
        app = get_app()
        module = app.module_manager.get(module_name) if app and app.module_manager else None
        if module is None:
            return None
        args, options = _rpc_arguments(params)
        appid = str(options.pop('appid', '') or '')
        if appid:
            bot = app.get_bot(appid)
            if bot is None:
                raise ValueError(f'机器人未运行或 appid 无效: {appid}')
            options.setdefault('token_manager', bot.token_manager)
            options.setdefault('sender', bot.sender)
        func = getattr(module, method, None)
        if func is None or not callable(func):
            raise ValueError(f'Image Hosting API 不存在: {method}')
        return await _call_rpc(func, args, options)

    async def stop(self):
        tasks = [task for task in (self._attach_task, self._watch_task) if task and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for name, process in list(self.processes.items()):
            await self._unregister_plugin(name)
            with contextlib.suppress(Exception):
                await process.stop()
        self.processes.clear()
        self.plugin_dirs.clear()
        self.plugin_adapters.clear()
        self._mtimes.clear()
