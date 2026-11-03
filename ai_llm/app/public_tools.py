"""Persistent public Agent tools, separate from plugin-private registrations."""
from __future__ import annotations

import ast
import asyncio
import copy
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading

import aiohttp
from jsonschema import Draft202012Validator, SchemaError

from .runtime import _PublicResolver, _public_url

_ID = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{0,47}$')
_LIMIT = 512 * 1024
_RUNNER = Path(__file__).with_name('tool_worker.py')
_RESERVED = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}

BUILTINS = [
    {'id': 'current_time', 'name': '当前时间', 'description': '查询机器人服务器的当前日期、时间和时区。',
     'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
     'code': 'def run(arguments, config):\n    from datetime import datetime\n    return {"time": datetime.now().astimezone().isoformat()}\n'},
    {'id': 'text_statistics', 'name': '文本统计', 'description': '统计一段文本的字符数、行数和英文单词数。',
     'parameters': {'type': 'object', 'properties': {'text': {'type': 'string', 'description': '待统计的文本'}}, 'required': ['text']},
     'code': 'def run(arguments, config):\n    text = arguments["text"]\n    return {"characters": len(text), "lines": len(text.splitlines()), "words": len(text.split())}\n'},
]


def validate_identifier(value: str) -> str:
    identifier = str(value or '').strip().lower()
    if not _ID.fullmatch(identifier) or identifier in _RESERVED:
        raise ValueError('工具 ID 以字母开头，仅含字母、数字、下划线，最长 48 字符')
    return identifier


def validate(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError('工具定义必须是 JSON 对象')
    identifier = validate_identifier(value.get('id'))
    code = value.get('code')
    if not isinstance(code, str) or not code.strip() or len(code.encode()) > 100000:
        raise ValueError('工具代码不能为空且不能超过 100KB')
    try:
        tree = ast.parse(code)
        compile(tree, '<agent-tool>', 'exec')
    except (SyntaxError, ValueError) as error:
        raise ValueError(f'Python 代码无效：{error}') from error
    functions = [item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == 'run']
    if len(functions) != 1:
        raise ValueError('工具须定义 run(arguments, config)，可使用 async def')
    signature = functions[0].args
    if len(signature.posonlyargs) + len(signature.args) != 2 or signature.kwonlyargs or signature.vararg or signature.kwarg:
        raise ValueError('工具须定义 run(arguments, config)，可使用 async def')
    parameters = value.get('parameters', {'type': 'object', 'properties': {}})
    if not isinstance(parameters, dict) or parameters.get('type') != 'object' or not isinstance(parameters.get('properties', {}), dict):
        raise ValueError('parameters 必须是 object 类型的 JSON Schema')
    def check_references(node, depth=0):
        if depth > 64:
            raise ValueError('参数 Schema 嵌套过深')
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {'$ref', '$dynamicRef'} and (not isinstance(item, str) or not item.startswith('#')):
                    raise ValueError('参数 Schema 仅支持内部引用，不能引用外部地址')
                check_references(item, depth + 1)
        elif isinstance(node, list):
            for item in node:
                check_references(item, depth + 1)
    check_references(parameters)
    try:
        Draft202012Validator.check_schema(parameters)
    except SchemaError as error:
        raise ValueError(f'参数 JSON Schema 无效：{error.message}') from error
    settings = value.get('config', {})
    if not isinstance(settings, dict):
        raise ValueError('工具配置必须是 JSON 对象')
    def strings(key):
        items = value.get(key, [])
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValueError(f'{key} 必须是字符串数组')
        return list(dict.fromkeys(item.strip() for item in items if item.strip()))[:100]
    result = {
        'id': identifier, 'name': str(value.get('name') or identifier).strip()[:100],
        'description': str(value.get('description') or '').strip()[:3000],
        'parameters': copy.deepcopy(parameters), 'code': code,
        'config': copy.deepcopy(settings), 'enabled': value.get('enabled') is True,
        'provider_ids': strings('provider_ids'), 'models': strings('models'),
        'timeout': min(120, max(1, int(value.get('timeout', 30)))),
    }
    if not result['description']:
        raise ValueError('请填写工具用途说明')
    origin = value.get('origin')
    if origin is not None:
        if not isinstance(origin, dict) or origin.get('kind') not in {'ai', 'download', 'import', 'manual'}:
            raise ValueError('无效的工具创建来源')
        result['origin'] = {'kind': origin['kind'],
                            'plugin': str(origin.get('plugin') or '').strip()[:128],
                            'provider_id': str(origin.get('provider_id') or '')[:128],
                            'model': str(origin.get('model') or '')[:256],
                            'created_at': max(0, int(origin.get('created_at') or 0))}
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > _LIMIT:
        raise ValueError('工具定义超过 512KB')
    return result


class PublicToolStore:
    def __init__(self, data_dir: str):
        self.directory = Path(data_dir).resolve() / 'public_tools'
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._items = {}
        self._errors = []
        self._processes = set()
        self._calls = set()
        self._slots = asyncio.Semaphore(4)
        self._closed = False
        for path in self.directory.glob('*.json'):
            try:
                if path.is_symlink() or path.stat().st_size > _LIMIT:
                    raise ValueError('无效的工具文件')
                item = validate(json.loads(path.read_text(encoding='utf-8-sig')))
                if path.stem != item['id']:
                    raise ValueError('文件名与工具 ID 不一致')
                self._items[item['id']] = item
            except (OSError, ValueError, TypeError) as error:
                self._errors.append({'file': path.name, 'error': str(error)[:200]})

    def catalog(self) -> list[dict]:
        with self._lock:
            return [{key: copy.deepcopy(value) for key, value in item.items() if key not in {'code', 'config'}}
                    | {'key': item['id'], 'function_name': 'tool_' + item['id'], 'source': 'public'}
                    for item in self._items.values()]

    def status(self) -> dict:
        return {'tools': self.catalog(), 'errors': copy.deepcopy(self._errors),
                'downloads': [{key: value for key, value in item.items() if key != 'code'} for item in BUILTINS]}

    def get(self, identifier: str) -> dict:
        with self._lock:
            if identifier not in self._items:
                raise ValueError('公用工具不存在')
            return copy.deepcopy(self._items[identifier])

    def save(self, value: dict, *, replace: bool = False) -> dict:
        item = validate(value)
        path = self.directory / (item['id'] + '.json')
        with self._lock:
            if path.exists() and not replace:
                raise ValueError('工具 ID 已存在，请编辑原工具或使用新的 ID')
            if path.is_symlink():
                raise ValueError('不允许覆盖符号链接')
            if replace and 'origin' not in value and item['id'] in self._items:
                previous_origin = self._items[item['id']].get('origin')
                if previous_origin:
                    item['origin'] = copy.deepcopy(previous_origin)
                    item = validate(item)
            descriptor, temporary = tempfile.mkstemp(prefix='.tool-', suffix='.tmp', dir=self.directory)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                    json.dump(item, file, ensure_ascii=False, indent=2)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            self._items[item['id']] = item
            self._errors = [error for error in self._errors if error['file'] != path.name]
        return copy.deepcopy(item)

    def require_available_id(self, identifier: str) -> str:
        identifier = validate_identifier(identifier)
        with self._lock:
            if (self.directory / (identifier + '.json')).exists() or identifier in self._items:
                raise ValueError('工具 ID 已存在，请使用新的 ID')
        return identifier

    def save_generated(self, value: dict, *, fixed_id: bool = False) -> dict:
        """Publish generated tools atomically without replacing another creator's work."""
        item = validate(value)
        original = item['id']
        with self._lock:
            suffix = 1
            while not fixed_id and ((self.directory / (item['id'] + '.json')).exists() or item['id'] in self._items):
                suffix += 1
                ending = f'_{suffix}'
                item['id'] = original[:48 - len(ending)] + ending
                if suffix > 10000:
                    raise ValueError('同名工具过多，请显式指定新的 tool_id')
            return self.save(item)

    def delete(self, identifier: str) -> None:
        with self._lock:
            item = self.get(identifier)
            (self.directory / (item['id'] + '.json')).unlink(missing_ok=True)
            self._items.pop(item['id'], None)

    def definitions(self, selected: list[str] | None = None, *, provider_id: str = '', model: str = '') -> list[dict]:
        with self._lock:
            items = copy.deepcopy(list(self._items.values()))
        return [{'type': 'function', 'function': {'name': 'tool_' + item['id'],
                 'description': item['description'], 'parameters': item['parameters']}}
                for item in items if item['enabled'] and (selected is None or item['id'] in selected)
                and (not item['provider_ids'] or provider_id in item['provider_ids'])
                and (not item['models'] or model in item['models'])]

    async def download(self, *, url: str = '', builtin: str = '') -> dict:
        if builtin:
            item = next((copy.deepcopy(item) for item in BUILTINS if item['id'] == builtin), None)
            if item is None:
                raise ValueError('内置工具不存在')
        else:
            endpoint = _public_url(url)
            connector = aiohttp.TCPConnector(resolver=_PublicResolver())
            async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=30), trust_env=False) as session:
                async with session.get(endpoint, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError(f'下载失败：HTTP {response.status}；请使用工具 JSON 的直接下载地址')
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(16384):
                        size += len(chunk)
                        if size > _LIMIT:
                            raise ValueError('工具文件超过 512KB')
                        chunks.append(chunk)
                    item = json.loads(b''.join(chunks).decode('utf-8-sig'))
        if not isinstance(item, dict):
            raise ValueError('下载内容必须是工具 JSON 对象')
        item['enabled'] = False
        item['config'] = {}  # Downloaded packages never carry executable credentials/settings.
        item['origin'] = {'kind': 'download'}
        return self.save(item)

    async def call(self, name: str, arguments: dict, *, provider_id: str = '', model: str = '', allow_disabled: bool = False):
        if self._closed:
            return {'ok': False, 'error': '工具服务已关闭'}
        identifier = name.removeprefix('tool_')
        item = self.get(identifier)
        if not allow_disabled and not any(row['function']['name'] == 'tool_' + identifier
                                          for row in self.definitions(provider_id=provider_id, model=model)):
            return {'ok': False, 'error': '公用工具未启用或不适用于当前接口/模型'}
        if not isinstance(arguments, dict):
            raise ValueError('工具参数必须是 JSON 对象')
        payload = json.dumps({'code': item['code'], 'parameters': item['parameters'], 'arguments': arguments,
                              'config': item['config']}, ensure_ascii=False, allow_nan=False).encode()
        if len(payload) > _LIMIT:
            raise ValueError('工具输入过大')
        async with self._slots:
            if self._closed:
                return {'ok': False, 'error': '工具服务已关闭'}
            environment = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH', 'LANG') if key in os.environ}
            options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-I', '-B', '-u', str(_RUNNER), cwd=str(self.directory), env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                **options,
            )
            self._processes.add(process)
            task = asyncio.current_task()
            self._calls.add(task)
            if self._closed:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                await process.communicate()
                self._processes.discard(process)
                self._calls.discard(task)
                return {'ok': False, 'error': '工具服务已关闭'}
            async def read(stream):
                chunks, size = [], 0
                while chunk := await stream.read(16384):
                    size += len(chunk)
                    if size > _LIMIT:
                        raise ValueError('工具输出超过 512KB')
                    chunks.append(chunk)
                return b''.join(chunks)
            async def communicate():
                readers = []
                process.stdin.write(payload)
                try:
                    await process.stdin.drain()
                    process.stdin.close()
                    readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
                    output, errors = await asyncio.gather(*readers)
                    await process.wait()
                    if process.returncode:
                        raise ValueError('工具进程执行失败：' + errors.decode('utf-8', errors='replace')[-500:])
                    return json.loads(output)
                finally:
                    for reader in readers:
                        if not reader.done():
                            reader.cancel()
                    if readers:
                        await asyncio.gather(*readers, return_exceptions=True)
            try:
                return await asyncio.wait_for(communicate(), timeout=item['timeout'])
            except TimeoutError:
                return {'ok': False, 'error': '公用工具执行超时'}
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                try:
                    # Drain paused pipes after killing the worker. wait() alone leaves
                    # Windows pipe transports open when output exceeded the limit.
                    await process.communicate()
                finally:
                    self._processes.discard(process)
                    self._calls.discard(task)

    async def close(self):
        self._closed = True
        current = asyncio.current_task()
        calls = [task for task in self._calls if task is not current and not task.done()]
        for task in calls:
            task.cancel()
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)
        processes = list(self._processes)
        for process in processes:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
        if processes:
            await asyncio.gather(*(process.wait() for process in processes))
