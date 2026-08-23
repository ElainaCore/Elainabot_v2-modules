"""SDK 适配器注册表。"""

from __future__ import annotations

from typing import Any

from .base import SdkAdapter


class AdapterRegistry:
    """按插件清单选择已注册的 SDK 适配器。"""

    def __init__(self, adapters=()):
        self._adapters: list[SdkAdapter] = list(adapters)

    def register(self, adapter: SdkAdapter) -> None:
        self._adapters.append(adapter)

    def resolve(self, manifest: dict[str, Any]) -> SdkAdapter | None:
        return next((item for item in self._adapters if item.supports(manifest)), None)

    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self._adapters)

    def source_suffixes(self) -> frozenset[str]:
        return frozenset(suffix for item in self._adapters for suffix in item.source_suffixes)
