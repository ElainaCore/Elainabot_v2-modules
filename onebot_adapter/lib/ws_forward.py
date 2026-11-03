"""OneBot 正向 WebSocket 的路由和监听管理。"""

from __future__ import annotations

import contextlib
from typing import Any

from aiohttp import web

from modules.onebot_adapter.lib.ws_protocol import WSConnection, WSConnectionHub

# aiohttp 路由注册后无法移除。路由处理器通过表查找当前 facade，支持配置热更新。
ROUTE_TABLE: dict[str, Any | None] = {}


class ForwardWebSocketServer:
    """管理挂载路由、独立端口和正向客户端生命周期。"""

    def __init__(self, *, owner: Any, entries: list[dict], connections: WSConnectionHub, log: Any) -> None:
        self.owner = owner
        self.entries = entries
        self.connections = connections
        self.log = log
        self.runners: list[web.AppRunner] = []
        self.standalone_status: dict[str, dict] = {}

    def attach(self, app: web.Application) -> list[str]:
        mounted = []
        for entry in self.entries:
            if int(entry.get('port', 0) or 0) > 0:
                continue
            path = entry['path']
            if path in ROUTE_TABLE:
                ROUTE_TABLE[path] = self.owner
                mounted.append(path)
                continue
            try:
                if path == '/':
                    install_wildcard_middleware(app)
                else:
                    app.router.add_get(path, make_ws_route_handler(path))
                ROUTE_TABLE[path] = self.owner
                mounted.append(path)
                suffix = ' (不校验路径)' if path == '/' else ''
                self.log.info(f'正向 WS 路由已挂载: {path}{suffix}')
            except (RuntimeError, ValueError):
                self.log.warning(f'正向 WS 路由注册跳过 (路由器已冻结, 需重启框架生效): {path}')
        return mounted

    async def start_standalone(self) -> None:
        """为配置独立端口的连接启动任意路径监听。"""
        for entry in self.entries:
            port = int(entry.get('port', 0) or 0)
            if port <= 0:
                continue
            name = entry.get('name', f':{port}')
            app = web.Application()
            app.router.add_get('/{tail:.*}', self._make_standalone_handler(entry))
            runner = web.AppRunner(app)
            try:
                await runner.setup()
                site = web.TCPSite(runner, '0.0.0.0', port)
                await site.start()
                self.runners.append(runner)
                self.standalone_status[name] = {'listening': True, 'error': ''}
                self.log.info(f'正向 WS 独立监听已启动: ws://0.0.0.0:{port} (任意路径)')
            except Exception as exc:
                self.standalone_status[name] = {'listening': False, 'error': str(exc)}
                self.log.error(f'正向 WS 独立监听启动失败 [:{port}]: {exc}')
                with contextlib.suppress(Exception):
                    await runner.cleanup()

    def _make_standalone_handler(self, entry: dict):
        async def handler(request: web.Request):
            return await self.handle(request, entry)

        return handler

    def detach(self) -> None:
        for path, server in list(ROUTE_TABLE.items()):
            if server is self.owner:
                ROUTE_TABLE[path] = None

    def entry_for(self, path: str) -> dict | None:
        return next((entry for entry in self.entries if entry['path'] == path), None)

    def status(self) -> dict[str, dict]:
        client_count = sum(not connection.is_client for connection in self.connections.clients)
        result = {}
        for entry in self.entries:
            if int(entry.get('port', 0) or 0) > 0:
                state = self.standalone_status.get(entry['name'], {'listening': False, 'error': '未启动'})
                result[entry['name']] = {
                    'mounted': state['listening'],
                    'clients': client_count,
                    'error': state['error'] if not state['listening'] else '',
                }
                continue
            mounted = ROUTE_TABLE.get(entry['path']) is self.owner
            result[entry['name']] = {
                'mounted': mounted,
                'clients': client_count,
                'error': '' if mounted else '路径未挂载 (需重启框架生效)',
            }
        return result

    async def handle(self, request: web.Request, entry: dict):
        token = entry.get('token', '')
        if token:
            auth = request.headers.get('Authorization', '')
            query_token = request.query.get('access_token', '')
            if auth not in {f'Bearer {token}', f'Token {token}'} and query_token != token:
                self.log.warning(f'正向 WS 鉴权失败: {request.remote}')
                return web.Response(status=401, text='Unauthorized')

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        appid = entry.get('appid', '')
        self_qq = self.connections.resolve_qq(appid)
        connection = WSConnection(ws, remote=str(request.remote), appid=appid, self_qq=self_qq)
        self.connections.add(connection)
        self.log.info(f'正向 WS 客户端已连接: {request.remote} (当前 {len(self.connections.clients)} 个)')
        await connection.send_str(self.connections.lifecycle_json(self_qq))

        try:
            async for message in ws:
                if message.type == web.WSMsgType.TEXT:
                    self.connections.dispatch_message(connection, message.data)
                elif message.type == web.WSMsgType.ERROR:
                    self.log.warning(f'正向 WS 错误: {ws.exception()}')
        except Exception as exc:
            self.log.warning(f'正向 WS 连接异常: {exc}')
        finally:
            self.connections.discard(connection)
            self.log.info(f'正向 WS 客户端已断开: {request.remote} (剩余 {len(self.connections.clients)} 个)')
        return ws

    async def stop(self) -> None:
        self.detach()
        for runner in self.runners:
            with contextlib.suppress(Exception):
                await runner.cleanup()
        self.runners.clear()
        self.standalone_status.clear()


@web.middleware
async def wildcard_ws_middleware(request: web.Request, handler):
    if (
        request.method == 'GET'
        and 'websocket' in request.headers.get('Upgrade', '').lower()
        and getattr(request.match_info, 'http_exception', None) is not None
    ):
        server = ROUTE_TABLE.get('/')
        if server is not None:
            entry = server._forward_entry_for('/')
            if entry is not None and entry.get('enable', True):
                return await server.handle_forward_ws(request, entry)
    return await handler(request)


def install_wildcard_middleware(app: web.Application) -> None:
    if wildcard_ws_middleware not in app.middlewares:
        app.middlewares.append(wildcard_ws_middleware)


def make_ws_route_handler(path: str):
    async def handler(request: web.Request):
        server = ROUTE_TABLE.get(path)
        if server is None:
            return web.Response(status=404, text='Not Found')
        entry = server._forward_entry_for(path)
        if entry is None or not entry.get('enable', True):
            return web.Response(status=404, text='Not Found')
        return await server.handle_forward_ws(request, entry)

    return handler
