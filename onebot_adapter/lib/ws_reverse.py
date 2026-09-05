"""OneBot 反向 WebSocket 连接管理。"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from modules.onebot_adapter.lib.ws_protocol import WSConnection, WSConnectionHub


class ReverseWebSocketClient:
    """建立并维护可自动重连的反向 WebSocket 连接。"""

    def __init__(self, *, entries: list[dict], connections: WSConnectionHub, log: Any) -> None:
        self.entries = entries
        self.connections = connections
        self.log = log
        self.tasks: list[asyncio.Task] = []
        self.session: aiohttp.ClientSession | None = None
        self.connection_status: dict[str, dict] = {}

    @staticmethod
    def normalize_url(url: str) -> str:
        value = url.strip()
        if value.startswith('http://'):
            return 'ws://' + value[7:]
        if value.startswith('https://'):
            return 'wss://' + value[8:]
        if not value.startswith(('ws://', 'wss://')):
            return 'ws://' + value
        return value

    async def start(self) -> None:
        if not self.entries:
            return
        self.session = aiohttp.ClientSession()
        for entry in self.entries:
            url = self.normalize_url(entry['url'])
            appid = entry.get('appid', '')
            if not url:
                continue
            task = asyncio.create_task(self.run_connection(url, entry))
            self.tasks.append(task)
            tag = f'{url} (appid={appid})' if appid else url
            self.log.info(f'反向 WS 连接任务已创建: {tag}')

    async def run_connection(self, url: str, entry: dict) -> None:
        appid = entry.get('appid', '')
        name = entry.get('name', url)
        token = entry.get('token', '')
        reconnect_interval = int(entry.get('reconnect_interval', 5) or 5)
        headers = {'Authorization': f'Bearer {token}'} if token else {}

        while True:
            self_qq = self.connections.resolve_qq(appid)
            headers['X-Self-ID'] = str(self_qq)
            headers['X-Client-Role'] = 'Universal'
            connection = None
            try:
                self.log.info(f'反向 WS 正在连接: {url}')
                if self.session is None:
                    return
                async with self.session.ws_connect(url, headers=headers) as ws:
                    connection = WSConnection(ws, remote=url, is_client=True, appid=appid, self_qq=self_qq)
                    self.connections.add(connection)
                    self.connection_status[name] = {'connected': True, 'error': ''}
                    self.log.info(
                        f'反向 WS 已连接: {url} '
                        f'(self_qq={self_qq}, appid={appid}, 当前 {len(self.connections.clients)} 个)'
                    )
                    await connection.send_str(self.connections.lifecycle_json(self_qq))

                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            self.connections.dispatch_message(connection, message.data)
                        elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                    self.connection_status[name] = {'connected': False, 'error': '连接断开'}
                    self.log.warning(
                        f'反向 WS 断开: {url}, appid={appid}, 当前 {len(self.connections.clients) - 1} 个)'
                    )
            except asyncio.CancelledError:
                self.connection_status[name] = {'connected': False, 'error': '已停止'}
                raise
            except Exception as exc:
                self.connection_status[name] = {'connected': False, 'error': str(exc)}
                self.log.warning(f'反向 WS 连接失败 [{url}], appid={appid}: {exc}')
            finally:
                if connection is not None:
                    self.connections.discard(connection)

            await asyncio.sleep(reconnect_interval)

    def status(self) -> dict[str, dict]:
        return {
            entry['name']: self.connection_status.get(entry['name'], {'connected': False, 'error': ''})
            for entry in self.entries
        }

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

        if self.session is not None:
            await self.session.close()
            self.session = None
