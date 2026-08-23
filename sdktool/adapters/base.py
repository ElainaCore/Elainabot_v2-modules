"""SDK 适配器基础协议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """描述 SDK 插件的启动参数。"""

    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)


class SdkAdapter:
    """定义一种 SDK 的插件入口和启动方式。"""

    name = ''
    default_entry = ''
    source_suffixes: frozenset[str] = frozenset()

    def supports(self, manifest: dict[str, Any]) -> bool:
        """判断适配器是否支持清单。"""
        raise NotImplementedError

    def available(self) -> bool:
        """判断当前环境能否运行 SDK。"""
        raise NotImplementedError

    def entry(self, manifest: dict[str, Any]) -> str:
        """返回插件入口文件。"""
        return str(manifest.get('entry') or self.default_entry)

    def build(self, manifest: dict[str, Any], plugin_dir: Path, entry: Path) -> LaunchSpec:
        """生成插件启动参数。"""
        raise NotImplementedError
