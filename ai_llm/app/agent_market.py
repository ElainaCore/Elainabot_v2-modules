"""Agent marketplace download using the framework's GitHub mirror pipeline."""
from __future__ import annotations

import io
import json
import os
import re
import zipfile

from web.tools._market.fetch import _download_file
from web.tools._market.shared import (
    _github_to_archive,
    _repo_raw_url,
    get_github_mirror,
)

from .agent_store import AgentFileError, AgentStore

CATALOG_URL = 'https://raw.githubusercontent.com/ElainaCore/Elaina-plugins/main/agents.json'
_SAFE_PATH = re.compile(r'^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9_.\-/]+$')


async def catalog() -> list[dict]:
    content = await _download_file(CATALOG_URL, mirror=get_github_mirror())
    if not content:
        raise AgentFileError('Agent 下载清单获取失败')
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentFileError('Agent 下载清单格式无效') from error
    rows = value if isinstance(value, list) else value.get('agents', [])
    result = []
    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, dict):
            continue
        agent_id = str(raw.get('id') or '').strip().casefold()
        github = str(raw.get('github') or '').strip().rstrip('/')
        path = str(raw.get('path') or '').strip().strip('/').replace('\\', '/')
        kind = str(raw.get('type') or 'file').strip().casefold()
        if (
            not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', agent_id)
            or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', github)
            or not _SAFE_PATH.fullmatch(path)
            or kind not in {'file', 'folder'}
        ):
            continue
        result.append({
            'id': agent_id,
            'name': str(raw.get('name') or agent_id).strip()[:100],
            'description': str(raw.get('description') or '').strip()[:500],
            'author': str(raw.get('author') or '').strip()[:100],
            'version': str(raw.get('version') or '').strip()[:50],
            'github': github,
            'branch': str(raw.get('branch') or 'main').strip()[:100],
            'path': path,
            'type': kind,
        })
    return result


def _folder_archive(content: bytes, path: str) -> bytes:
    try:
        source = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise AgentFileError('Agent 仓库压缩包格式无效') from error
    output_bytes = io.BytesIO()
    with source, zipfile.ZipFile(output_bytes, 'w', zipfile.ZIP_DEFLATED) as output:
        names = source.namelist()
        roots = {name.split('/')[0] for name in names if '/' in name and name.split('/')[0]}
        root = (next(iter(roots)) + '/') if len(roots) == 1 else ''
        prefix = root + path.rstrip('/') + '/'
        selected = [name for name in names if name.startswith(prefix) and not name.endswith('/')]
        if not selected:
            raise AgentFileError(f'仓库内未找到 Agent 文件夹：{path}')
        for name in selected:
            relative = name[len(prefix):]
            if relative:
                output.writestr(relative, source.read(name))
    return output_bytes.getvalue()


async def install(store: AgentStore, agent_id: str) -> dict:
    item = next((row for row in await catalog() if row['id'] == str(agent_id or '')), None)
    if item is None:
        raise AgentFileError('Agent 不在下载清单中')
    if item['type'] == 'file':
        url = _repo_raw_url(item['github'], item['path'], item['branch'])
        content = await _download_file(url, mirror=get_github_mirror())
        if not content:
            raise AgentFileError('Agent 文件下载失败')
        installed = store.upload(os.path.basename(item['path']), content)
    else:
        archive_url = _github_to_archive(item['github'], item['branch'])
        content = await _download_file(archive_url, mirror=get_github_mirror())
        if not content:
            raise AgentFileError('Agent 仓库下载失败')
        installed = store.install_archive(_folder_archive(content, item['path']))
    if installed['id'] != item['id']:
        store.delete(installed['id'])
        raise AgentFileError('下载清单 ID 与 Agent 文件声明不一致')
    return {**installed, 'market': item}
