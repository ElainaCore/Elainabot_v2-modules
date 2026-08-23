"""Node.js SDK 适配器。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .base import LaunchSpec, SdkAdapter


class NodeAdapter(SdkAdapter):
    """适配 Node.js 插件和 elainabot-sdk。"""

    name = 'node'
    default_entry = 'main.js'
    source_suffixes = frozenset({'.js', '.cjs', '.mjs', '.json'})
    runtimes = frozenset({'node', 'javascript', 'js'})
    sdk_names = frozenset({'', 'node', 'elainabot', 'elainabot-js', 'elainabot-sdk'})

    def __init__(self, root: Path):
        self.root = root

    def supports(self, manifest: dict[str, Any]) -> bool:
        return (
            str(manifest.get('runtime', 'node')).lower() in self.runtimes
            and str(manifest.get('sdk', '')).lower() in self.sdk_names
        )

    def available(self) -> bool:
        return shutil.which('node') is not None

    def build(self, manifest: dict[str, Any], plugin_dir: Path, entry: Path) -> LaunchSpec:
        node = shutil.which('node')
        if node is None:
            raise RuntimeError('未找到 Node.js 运行环境')
        raw = manifest.get('command')
        if raw is None:
            command = [node, str(entry)]
        elif isinstance(raw, str):
            command = [raw, str(entry)]
        elif isinstance(raw, list):
            command = [str(entry) if str(item) == '{entry}' else str(item) for item in raw]
        else:
            raise ValueError('command 必须是字符串或数组')
        sdk_path = self.root / 'sdk'
        node_path = str(sdk_path)
        if os.environ.get('NODE_PATH'):
            node_path += os.pathsep + os.environ['NODE_PATH']
        return LaunchSpec(tuple(command), {'ELAINA_JS_SDK': str(sdk_path), 'NODE_PATH': node_path})
