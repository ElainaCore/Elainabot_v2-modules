"""管理文件与文件夹形式的模型工具。"""
from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from urllib.parse import quote

import aiohttp

from .runtime import RuntimeCapabilityError, _PublicResolver, _public_url

_MODEL_TOOL_ID = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
_TOOL_NAME = re.compile(r'[^A-Za-z0-9_-]+')
_DANGEROUS_IMPORTS = {
    'ctypes', 'importlib', 'os', 'pathlib', 'pickle', 'shutil', 'socket',
    'subprocess', 'sys',
}
_ENTRY_LIMIT = 512 * 1024
_UPLOAD_LIMIT = 20 * 1024 * 1024
_FILE_LIMIT = 200


class ModelToolFileError(ValueError):
    pass


class ModelToolStore:
    def __init__(self, data_dir: str):
        self.directory = os.path.abspath(os.path.join(data_dir or '.', 'model_tools'))
        os.makedirs(self.directory, exist_ok=True)
        self._catalog_cache_key = None
        self._catalog_cache: list[dict] = []

    @staticmethod
    def _metadata(source: str, filename: str) -> dict:
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as error:
            raise ModelToolFileError(f'Python 语法错误：{error.msg}') from error
        metadata = None
        has_run = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'run':
                has_run = True
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [item.name.split('.')[0] for item in node.names]
                if any(name in _DANGEROUS_IMPORTS for name in names):
                    raise ModelToolFileError('模型工具禁止导入危险模块')
            if isinstance(node, ast.Name) and node.id in {
                '__import__', 'compile', 'eval', 'exec', 'input', 'open',
            }:
                raise ModelToolFileError(f'模型工具禁止使用 {node.id}')
            if isinstance(node, ast.Attribute) and node.attr.startswith('__'):
                safe_type_name = (
                    node.attr == '__name__'
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == 'type'
                    and len(node.value.args) == 1
                    and not node.value.keywords
                )
                if not safe_type_name:
                    raise ModelToolFileError('模型工具禁止访问双下划线属性')
            if (
                isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == 'TOOL'
            ):
                try:
                    metadata = ast.literal_eval(node.value)
                except (TypeError, ValueError) as error:
                    raise ModelToolFileError('TOOL 必须是静态字典') from error
        if not isinstance(metadata, dict) or not has_run:
            raise ModelToolFileError('模型工具必须定义 TOOL 字典和 run(arguments, context)')
        tool_id = str(metadata.get('id') or '').strip().casefold()
        if not _MODEL_TOOL_ID.fullmatch(tool_id):
            raise ModelToolFileError('模型工具 ID 格式无效')
        parameters = metadata.get('parameters') or {'type': 'object', 'properties': {}}
        if not isinstance(parameters, dict) or parameters.get('type') != 'object':
            raise ModelToolFileError('parameters 必须是 object JSON Schema')
        description = ' '.join(str(metadata.get('description') or '').split())[:120]
        if not description:
            raise ModelToolFileError('模型工具 description 不能为空')
        return {
            'id': tool_id,
            'name': ' '.join(str(metadata.get('name') or tool_id).split())[:100],
            'description': description,
            'parameters': copy.deepcopy(parameters),
            'enabled': bool(metadata.get('enabled', True)),
        }

    @staticmethod
    def _entry(path: str) -> str:
        return os.path.join(path, 'tool.py') if os.path.isdir(path) else path

    def _catalog_signature(self):
        signature = []
        try:
            entries = os.scandir(self.directory)
        except OSError:
            return ()
        with entries:
            for entry in entries:
                path = os.path.join(entry.path, 'tool.py') if entry.is_dir(follow_symlinks=False) else entry.path
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                signature.append((entry.name, stat.st_mtime_ns, stat.st_size))
        return tuple(sorted(signature))

    def _invalidate_catalog(self) -> None:
        self._catalog_cache_key = None
        self._catalog_cache = []

    def catalog(self) -> list[dict]:
        signature = self._catalog_signature()
        if signature == self._catalog_cache_key:
            return copy.deepcopy(self._catalog_cache)
        result = []
        for name in sorted(os.listdir(self.directory)):
            path = os.path.join(self.directory, name)
            entry = self._entry(path)
            valid_name = name.endswith('.py') if os.path.isfile(path) else bool(_MODEL_TOOL_ID.fullmatch(name))
            if not valid_name or not os.path.isfile(entry):
                continue
            try:
                with open(entry, encoding='utf-8') as file:
                    source = file.read(_ENTRY_LIMIT + 1)
                if len(source.encode('utf-8')) > _ENTRY_LIMIT:
                    continue
                metadata = self._metadata(source, entry)
                result.append({
                    **metadata,
                    'filename': name,
                    'entry': 'tool.py' if os.path.isdir(path) else name,
                    'kind': 'folder' if os.path.isdir(path) else 'file',
                    'source': 'model_tool',
                })
            except (ModelToolFileError, OSError, UnicodeDecodeError):
                continue
        self._catalog_cache_key = signature
        self._catalog_cache = result
        return copy.deepcopy(result)

    @staticmethod
    def _validate_tree(root: str) -> None:
        count = 0
        total = 0
        for base, directories, names in os.walk(root):
            directories[:] = [
                name for name in directories
                if name not in {'__pycache__', '.git'} and not name.startswith('.')
            ]
            for name in names:
                path = os.path.join(base, name)
                if os.path.islink(path):
                    raise ModelToolFileError('模型工具不允许符号链接')
                count += 1
                total += os.path.getsize(path)
                if count > _FILE_LIMIT or total > _UPLOAD_LIMIT:
                    raise ModelToolFileError('模型工具文件数量或总大小超限')

    def install_archive(self, content: bytes) -> dict:
        if not content or len(content) > _UPLOAD_LIMIT:
            raise ModelToolFileError('模型工具压缩包为空或超过 20MB')
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as error:
            raise ModelToolFileError('模型工具压缩包格式无效') from error
        staging_root = tempfile.mkdtemp(prefix='.model-tool-upload-', dir=self.directory)
        try:
            with archive:
                members = [item for item in archive.infolist() if not item.is_dir()]
                if not members or len(members) > _FILE_LIMIT:
                    raise ModelToolFileError('模型工具压缩包文件数量无效')
                if sum(item.file_size for item in members) > _UPLOAD_LIMIT:
                    raise ModelToolFileError('模型工具解压后不能超过 20MB')
                for item in members:
                    normalized = item.filename.replace('\\', '/').strip('/')
                    parts = [part for part in normalized.split('/') if part not in {'', '.'}]
                    if not parts or '..' in parts or os.path.isabs(item.filename):
                        raise ModelToolFileError('模型工具压缩包包含不安全路径')
                    if (item.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ModelToolFileError('模型工具压缩包不能包含符号链接')
                    target = os.path.abspath(os.path.join(staging_root, *parts))
                    if not target.startswith(staging_root + os.sep):
                        raise ModelToolFileError('模型工具压缩包包含越界路径')
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with archive.open(item) as source, open(target, 'wb') as output:
                        shutil.copyfileobj(source, output)
            root = staging_root
            if not os.path.isfile(os.path.join(root, 'tool.py')):
                children = [os.path.join(root, name) for name in os.listdir(root)]
                if len(children) == 1 and os.path.isdir(children[0]):
                    root = children[0]
            self._validate_tree(root)
            entry = os.path.join(root, 'tool.py')
            if not os.path.isfile(entry):
                raise ModelToolFileError('文件夹模型工具根目录必须包含 tool.py')
            with open(entry, encoding='utf-8') as file:
                metadata = self._metadata(file.read(_ENTRY_LIMIT + 1), entry)
            destination = os.path.join(self.directory, metadata['id'])
            if os.path.exists(destination) or os.path.exists(destination + '.py'):
                raise ModelToolFileError(f"模型工具 {metadata['id']} 已存在")
            os.replace(root, destination)
            self._invalidate_catalog()
            return {
                **metadata, 'filename': metadata['id'], 'entry': 'tool.py',
                'kind': 'folder', 'source': 'model_tool',
            }
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    def upload(self, filename: str, content: bytes) -> dict:
        name = os.path.basename(str(filename or ''))
        if name.casefold().endswith('.zip'):
            return self.install_archive(content)
        if not name.casefold().endswith('.py') or len(content) > _ENTRY_LIMIT:
            raise ModelToolFileError('仅支持不超过 512KB 的 .py 或模型工具 ZIP')
        try:
            source = content.decode('utf-8-sig')
        except UnicodeDecodeError as error:
            raise ModelToolFileError('模型工具文件必须使用 UTF-8 编码') from error
        metadata = self._metadata(source, name)
        destination = os.path.join(self.directory, metadata['id'] + '.py')
        if os.path.isdir(os.path.join(self.directory, metadata['id'])):
            raise ModelToolFileError(f"模型工具 {metadata['id']} 已存在")
        with open(destination, 'w', encoding='utf-8', newline='\n') as file:
            file.write(source)
        self._invalidate_catalog()
        return {
            **metadata, 'filename': metadata['id'] + '.py',
            'entry': metadata['id'] + '.py', 'kind': 'file', 'source': 'model_tool',
        }

    def delete(self, tool_id: str) -> bool:
        tool_id = str(tool_id or '').strip().casefold()
        if not _MODEL_TOOL_ID.fullmatch(tool_id):
            return False
        for path in (
            os.path.join(self.directory, tool_id + '.py'),
            os.path.join(self.directory, tool_id),
        ):
            if os.path.isfile(path):
                os.remove(path)
                self._invalidate_catalog()
                return True
            if os.path.isdir(path):
                shutil.rmtree(path)
                self._invalidate_catalog()
                return True
        return False

    @staticmethod
    def tool_name(tool_id: str) -> str:
        return _TOOL_NAME.sub('_', 'tool_' + str(tool_id))[:64]

    def tools(self) -> list[dict]:
        return [{
            'type': 'function',
            'function': {
                'name': self.tool_name(item['id']),
                'description': item['description'],
                'parameters': copy.deepcopy(item['parameters']),
            },
        } for item in self.catalog() if item.get('enabled')]

    async def _http_json(self, url: str, params: dict | None = None):
        target = _public_url(url)
        connector = aiohttp.TCPConnector(resolver=_PublicResolver(), ttl_dns_cache=0)
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(target, params=params or {}, allow_redirects=False) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeCapabilityError(f'远端服务返回 HTTP {response.status}')
                return json.loads(await response.text())

    async def call(self, name: str, arguments: dict, context: dict | None = None):
        item = next((row for row in self.catalog() if self.tool_name(row['id']) == name), None)
        if item is None:
            return None
        path = os.path.join(self.directory, item['filename'])
        if item['kind'] == 'folder':
            path = os.path.join(path, 'tool.py')
        with open(path, encoding='utf-8') as file:
            self._metadata(file.read(_ENTRY_LIMIT + 1), path)
        module_name = 'ai_model_tool_' + hashlib.sha256(path.encode()).hexdigest()[:16]
        search = [os.path.dirname(path)] if item['kind'] == 'folder' else None
        spec = importlib.util.spec_from_file_location(module_name, path, submodule_search_locations=search)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            runtime_context = dict(context or {})
            runtime_context.update({'http_json': self._http_json, 'quote': quote})
            value = module.run(arguments if isinstance(arguments, dict) else {}, runtime_context)
            if asyncio.iscoroutine(value):
                value = await asyncio.wait_for(value, timeout=45)
            return value if isinstance(value, dict) else {'ok': True, 'result': value}
        except asyncio.TimeoutError:
            return {'ok': False, 'error': '模型工具执行超时'}
        except Exception as error:
            return {'ok': False, 'error': str(error)[:300]}
        finally:
            sys.modules.pop(module_name, None)
