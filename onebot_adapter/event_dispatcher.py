"""框架事件到 OneBot 事件的运行时分发。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from modules.onebot_adapter.action_context import ActionContext
from modules.onebot_adapter.lib.event_converter import convert_lifecycle_event, convert_message_event


class GroupNameResolver:
    """读取群名称并合并同一群的并发刷新请求。"""

    CACHE_TTL = 300
    EMPTY_CACHE_TTL = 3600

    def __init__(
        self,
        log: Any,
        *,
        cache: dict[tuple[str, str], tuple[float, str]] | None = None,
        locks: dict[tuple[str, str], asyncio.Lock] | None = None,
    ) -> None:
        self.log = log
        self.cache = cache if cache is not None else {}
        self.locks = locks if locks is not None else {}

    async def resolve(self, event: Any, bot: Any) -> str:
        group_id = str(event.group_id or '')
        if not event.is_group or not group_id:
            return ''

        cache_key = (str(event.appid or ''), group_id)
        now = time.monotonic()
        cached = self.cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        lock = self.locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self.cache.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]

            group_name = await self._load_group_name(bot, group_id)
            ttl = self.CACHE_TTL if group_name else self.EMPTY_CACHE_TTL
            self.cache[cache_key] = (now + ttl, group_name)
            return group_name

    async def _load_group_name(self, bot: Any, group_id: str) -> str:
        sender = getattr(bot, 'sender', None)
        group_name = await self._call_sender(
            sender,
            'get_group_record',
            group_id,
            failure_message='读取 OneBot 上报群名失败',
        )
        if group_name:
            return group_name
        return await self._call_sender(
            sender,
            'get_group_info',
            group_id,
            failure_message='刷新 OneBot 上报群名失败',
        )

    async def _call_sender(self, sender: Any, method_name: str, group_id: str, *, failure_message: str) -> str:
        method = getattr(sender, method_name, None)
        if method is None:
            return ''
        try:
            result = await method(group_id)
            if isinstance(result, dict):
                return str(result.get('group_name') or '')
        except Exception as exc:
            self.log.debug(f'{failure_message}: group_id={group_id}, error={exc}')
        return ''


class OneBotEventDispatcher:
    """维护事件侧运行时状态，转换事件并推送到活动传输。"""

    def __init__(
        self,
        *,
        action_context: ActionContext,
        id_mapper: Any,
        group_names: GroupNameResolver,
    ) -> None:
        self.action_context = action_context
        self.id_mapper = id_mapper
        self.group_names = group_names

    async def dispatch(self, event: Any, bot: Any, *, ws_server: Any = None, http_webhook: Any = None) -> dict | None:
        has_ws = ws_server is not None and ws_server.has_clients
        has_webhook = http_webhook is not None and http_webhook.has_targets
        if not has_ws and not has_webhook:
            return None

        appid = str(event.appid or '')
        self._update_services(appid, bot)
        self._update_identity(appid, bot, ws_server)
        self_qq = self.action_context.qq_map.get(appid, self.action_context.default_qq)
        self_qq = self_qq or self.action_context.default_qq

        if event.is_lifecycle:
            converted = await convert_lifecycle_event(event, self.id_mapper, self_qq)
        else:
            group_name = await self.group_names.resolve(event, bot)
            converted = await convert_message_event(
                event,
                self.id_mapper,
                self_qq,
                group_name=group_name,
            )

        if converted is not None:
            if has_ws:
                await ws_server.broadcast(converted, appid=appid)
            if has_webhook:
                http_webhook.push(converted, appid=appid)
        return converted

    def _update_services(self, appid: str, bot: Any) -> None:
        if not appid:
            return
        sender = getattr(bot, 'sender', None)
        if sender is not None:
            self.action_context.senders[appid] = sender
        log_service = getattr(bot, 'log_service', None)
        if log_service is not None:
            self.action_context.log_services[appid] = log_service

    def _update_identity(self, appid: str, bot: Any, ws_server: Any) -> None:
        if not appid or appid in self.action_context.qq_map:
            return
        robot_qq = getattr(bot, 'robot_qq', '') or ''
        if not robot_qq:
            return

        qq = int(robot_qq)
        self.action_context.qq_map[appid] = qq
        if not self.action_context.default_qq:
            self.action_context.default_qq = qq
        if ws_server is not None:
            update_identity = getattr(ws_server, 'update_identity', None)
            if update_identity is not None:
                update_identity(self.action_context.qq_map, self.action_context.default_qq)
            else:
                ws_server.qq_map = self.action_context.qq_map
                ws_server._default_qq = self.action_context.default_qq
