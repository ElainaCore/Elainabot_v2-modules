"""通过框架镜像管理模型工具市场。"""
from __future__ import annotations

import io
import asyncio
import copy
import json
import os
import re
import time
import zipfile

from web.tools._market.fetch import _download_file
from web.tools._market.shared import (
    _github_to_archive,
    _repo_raw_url,
    get_github_mirror,
)

from .model_tool_store import ModelToolFileError, ModelToolStore

CATALOG_URL = 'https://raw.githubusercontent.com/ElainaCore/Elaina-plugins/main/tools.json'
_SAFE_PATH = re.compile(r'^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9_.\-/]+$')
_CACHE_TTL = 300
_catalog_cache: list[dict] = []
_catalog_cached_at = 0.0
_catalog_lock: asyncio.Lock | None = None

def _summary(value, limit: int = 120) -> str:
    return ' '.join(str(value or '').split())[:limit]


async def catalog(force: bool = False) -> list[dict]:
    global _catalog_cache, _catalog_cached_at, _catalog_lock
    if not force and _catalog_cache and time.monotonic() - _catalog_cached_at < _CACHE_TTL:
        return copy.deepcopy(_catalog_cache)
    if _catalog_lock is None:
        _catalog_lock = asyncio.Lock()
    async with _catalog_lock:
        if not force and _catalog_cache and time.monotonic() - _catalog_cached_at < _CACHE_TTL:
            return copy.deepcopy(_catalog_cache)
        content = await _download_file(CATALOG_URL, mirror=get_github_mirror())
        if not content:
            raise ModelToolFileError('模型工具清单获取失败')
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelToolFileError('模型工具清单格式无效') from error
        rows = value if isinstance(value, list) else value.get('tools', [])
        result = []
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            tool_id = str(raw.get('id') or '').strip().casefold()
            github = str(raw.get('github') or '').strip().rstrip('/')
            path = str(raw.get('path') or '').strip().strip('/').replace('\\', '/')
            kind = str(raw.get('type') or 'file').strip().casefold()
            if (
                not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', tool_id)
                or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', github)
                or not _SAFE_PATH.fullmatch(path)
                or kind not in {'file', 'folder'}
            ):
                continue
            result.append({
                'id': tool_id, 'name': _summary(raw.get('name') or tool_id, 100),
                'description': _summary(raw.get('description')),
                'version': str(raw.get('version') or '').strip()[:50],
                'github': github, 'branch': str(raw.get('branch') or 'main').strip()[:100],
                'path': path, 'type': kind,
            })
        _catalog_cache = result
        _catalog_cached_at = time.monotonic()
        return copy.deepcopy(result)


def _folder_archive(content: bytes, path: str) -> bytes:
    try:
        source = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise ModelToolFileError('模型工具仓库压缩包格式无效') from error
    output_bytes = io.BytesIO()
    with source, zipfile.ZipFile(output_bytes, 'w', zipfile.ZIP_DEFLATED) as output:
        names = source.namelist()
        roots = {name.split('/')[0] for name in names if '/' in name and name.split('/')[0]}
        root = (next(iter(roots)) + '/') if len(roots) == 1 else ''
        prefix = root + path.rstrip('/') + '/'
        selected = [name for name in names if name.startswith(prefix) and not name.endswith('/')]
        if not selected:
            raise ModelToolFileError(f'仓库内未找到模型工具文件夹：{path}')
        for name in selected:
            relative = name[len(prefix):]
            if relative:
                output.writestr(relative, source.read(name))
    return output_bytes.getvalue()


async def install(store: ModelToolStore, tool_id: str) -> dict:
    item = next((row for row in await catalog() if row['id'] == str(tool_id or '')), None)
    if item is None:
        raise ModelToolFileError('模型工具不在清单中')
    if item['type'] == 'file':
        url = _repo_raw_url(item['github'], item['path'], item['branch'])
        content = await _download_file(url, mirror=get_github_mirror())
        if not content:
            raise ModelToolFileError('模型工具文件下载失败')
        installed = store.upload(os.path.basename(item['path']), content)
    else:
        archive_url = _github_to_archive(item['github'], item['branch'])
        content = await _download_file(archive_url, mirror=get_github_mirror())
        if not content:
            raise ModelToolFileError('模型工具仓库下载失败')
        installed = store.install_archive(_folder_archive(content, item['path']))
    if installed['id'] != item['id']:
        store.delete(installed['id'])
        raise ModelToolFileError('清单 ID 与模型工具声明不一致')
    return {**installed, 'market': item}
