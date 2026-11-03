"""OneBot WebSocket 连接与协议处理。

本模块只管理已建立连接上的公共行为：action 请求、事件广播和心跳。
正向监听与反向连接的建立分别由 ws_forward.py 和 ws_reverse.py 负责。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from typing import Any

_B64_RE = re.compile(r'(base64://|"base64://|data:image[^,]*,)[A-Za-z0-9+/=]{64,}')


def mask_base64(value: str) -> str:
    """隐藏日志中的大段 base64 数据。"""
    return _B64_RE.sub(lambda match: match.group(1) + '<...base64...>', value)


class WSConnection:
    """统一 aiohttp 服务端和客户端 WebSocket 的最小接口。"""

    __slots__ = ('_ws', 'is_client', 'remote', 'appid', 'self_qq')

    def __init__(self, ws, remote: str = '', is_client: bool = False, appid: str = '', self_qq: int = 0):
        self._ws = ws
        self.is_client = is_client
        self.remote = remote
        self.appid = appid
        self.self_qq = self_qq

    async def send_str(self, data: str) -> None:
        await self._ws.send_str(data)

    async def close(self) -> None:
        await self._ws.close()

    @property
    def closed(self) -> bool:
        return self._ws.closed if hasattr(self._ws, 'closed') else False

    @property
    def _is_client(self) -> bool:
        """兼容重构前的内部属性名。"""
        return self.is_client


class WSConnectionHub:
    """管理所有已建立的连接及其 OneBot 协议行为。"""

    def __init__(
        self,
        *,
        heartbeat_interval: int,
        on_action,
        default_qq: int,
        qq_map: dict[str, int],
        log: Any,
        debug: bool,
    ) -> None:
        self.heartbeat_interval = heartbeat_interval
        self.on_action = on_action
        self.default_qq = default_qq
        self.qq_map = qq_map
        self.log = log
        self.debug = debug
        self.clients: set[WSConnection] = set()
        self._heartbeat_task: asyncio.Task | None = None
        self._message_tasks: set[asyncio.Task] = set()

    @property
    def has_clients(self) -> bool:
        return bool(self.clients)

    def resolve_qq(self, appid: str = '') -> int:
        return self.qq_map.get(appid, self.default_qq) or self.default_qq

    def add(self, connection: WSConnection) -> None:
        self.clients.add(connection)

    def discard(self, connection: WSConnection) -> None:
        self.clients.discard(connection)

    @staticmethod
    def lifecycle_json(self_qq: int, sub_type: str = 'connect') -> str:
        return json.dumps(
            {
                'time': int(time.time()),
                'self_id': self_qq,
                'post_type': 'meta_event',
                'meta_event_type': 'lifecycle',
                'sub_type': sub_type,
            },
            ensure_ascii=False,
        )

    def dispatch_message(self, connection: WSConnection, raw: str) -> None:
        """独立处理 action，避免阻塞连接的接收循环。"""
        task = asyncio.create_task(self.handle_message(connection, raw))
        self._message_tasks.add(task)
        task.add_done_callback(self._message_tasks.discard)

    async def handle_message(self, connection: WSConnection, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.log.warning(f'无法解析的 WS 消息: {raw[:200]}')
            return

        action = data.get('action', '')
        params = data.get('params', {})
        echo = data.get('echo')
        if not action:
            return

        if self.debug:
            serialized = json.dumps(params, ensure_ascii=False)
            self.log.info(f'[WS←] action={action} params={mask_base64(serialized)}')

        try:
            result = await self.on_action(action, params, echo, connection.appid)
        except Exception as exc:
            import traceback

            trace = ''.join(traceback.format_exception(exc))
            self.log.error(f'onebot._handle_message.action.{action}:{exc}\n{trace}')
            result = {
                'status': 'failed',
                'retcode': -1,
                'data': None,
                'msg': str(exc),
                'wording': str(exc),
            }
            if echo is not None:
                result['echo'] = echo

        response = json.dumps(result, ensure_ascii=False)
        if self.debug:
            self.log.info(f'[WS→] resp={response}')
        with contextlib.suppress(Exception):
            await connection.send_str(response)

    async def broadcast(self, event: dict, appid: str = '') -> None:
        """向匹配 appid 的连接推送事件。"""
        if not self.clients:
            return
        data = json.dumps(event, ensure_ascii=False)
        if self.debug and event.get('post_type') != 'meta_event':
            self.log.info(f'[WS→] {data}')

        dead = set()
        for connection in list(self.clients):
            if connection.appid and appid and connection.appid != appid:
                continue
            try:
                await connection.send_str(data)
            except Exception:
                dead.add(connection)
        self.clients.difference_update(dead)

    def start_heartbeat(self) -> None:
        if self.heartbeat_interval > 0 and self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self.heartbeat_loop())

    async def heartbeat_loop(self) -> None:
        """定期向每个连接发送使用其 self_id 的心跳。"""
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            dead = []
            for connection in list(self.clients):
                heartbeat = json.dumps(
                    {
                        'time': int(time.time()),
                        'self_id': connection.self_qq or self.default_qq,
                        'post_type': 'meta_event',
                        'meta_event_type': 'heartbeat',
                        'status': {'online': True, 'good': True},
                        'interval': self.heartbeat_interval * 1000,
                    }
                )
                try:
                    await connection.send_str(heartbeat)
                except Exception:
                    dead.append(connection)
            for connection in dead:
                self.clients.discard(connection)

    async def stop(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None

        if self._message_tasks:
            await asyncio.gather(*self._message_tasks, return_exceptions=True)
            self._message_tasks.clear()

        for connection in list(self.clients):
            with contextlib.suppress(Exception):
                await connection.close()
        self.clients.clear()
