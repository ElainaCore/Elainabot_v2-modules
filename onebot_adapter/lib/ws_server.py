"""OneBot 11 WebSocket 外观，组合正向、反向与公共协议组件。"""

from __future__ import annotations

from aiohttp import web

from modules.onebot_adapter.lib import ws_forward, ws_protocol
from modules.onebot_adapter.lib.ws_forward import ForwardWebSocketServer
from modules.onebot_adapter.lib.ws_protocol import WSConnection as _WSWrapper
from modules.onebot_adapter.lib.ws_protocol import WSConnectionHub
from modules.onebot_adapter.lib.ws_reverse import ReverseWebSocketClient

# 兼容旧模块中可导入的内部符号，路由表本身仍由正向传输层维护。
_ROUTE_TABLE = ws_forward.ROUTE_TABLE
_install_wildcard_middleware = ws_forward.install_wildcard_middleware
_make_ws_route_handler = ws_forward.make_ws_route_handler
_wildcard_ws_middleware = ws_forward.wildcard_ws_middleware
_mask_b64 = ws_protocol.mask_base64


class OneBotWSServer:
    """保持原有 API 的 WebSocket 子系统外观。"""

    __slots__ = ('_connections', '_forward', '_reverse')

    def __init__(
        self,
        *,
        heartbeat_interval,
        on_action,
        default_qq=0,
        qq_map=None,
        log,
        forward_entries=None,
        reverse_entries=None,
        debug=False,
    ) -> None:
        self._connections = WSConnectionHub(
            heartbeat_interval=heartbeat_interval,
            on_action=on_action,
            default_qq=default_qq,
            qq_map=qq_map if qq_map is not None else {},
            log=log,
            debug=debug,
        )
        self._forward = ForwardWebSocketServer(
            owner=self,
            entries=forward_entries or [],
            connections=self._connections,
            log=log,
        )
        self._reverse = ReverseWebSocketClient(
            entries=reverse_entries or [],
            connections=self._connections,
            log=log,
        )

    @property
    def qq_map(self) -> dict[str, int]:
        return self._connections.qq_map

    @qq_map.setter
    def qq_map(self, value: dict[str, int]) -> None:
        self._connections.qq_map = value

    @property
    def _default_qq(self) -> int:
        return self._connections.default_qq

    @_default_qq.setter
    def _default_qq(self, value: int) -> None:
        self._connections.default_qq = value

    @property
    def _clients(self) -> set[_WSWrapper]:
        return self._connections.clients

    @property
    def _forward_entries(self) -> list[dict]:
        return self._forward.entries

    @property
    def _reverse_entries(self) -> list[dict]:
        return self._reverse.entries

    @property
    def _reverse_status(self) -> dict[str, dict]:
        return self._reverse.connection_status

    @property
    def _standalone_status(self) -> dict[str, dict]:
        return self._forward.standalone_status

    @property
    def has_clients(self) -> bool:
        return self._connections.has_clients

    def resolve_qq(self, appid: str = '') -> int:
        return self._connections.resolve_qq(appid)

    def update_identity(self, qq_map: dict[str, int], default_qq: int) -> None:
        """更新运行时机器人标识，避免调用方写入内部字段。"""
        self.qq_map = qq_map
        self._default_qq = default_qq

    def _lifecycle_json(self, self_qq: int, sub_type: str = 'connect') -> str:
        return self._connections.lifecycle_json(self_qq, sub_type)

    def attach(self, app: web.Application) -> list[str]:
        return self._forward.attach(app)

    async def start_standalone(self) -> None:
        await self._forward.start_standalone()

    def detach(self) -> None:
        self._forward.detach()

    def _forward_entry_for(self, path: str) -> dict | None:
        return self._forward.entry_for(path)

    @staticmethod
    def _normalize_ws_url(url: str) -> str:
        return ReverseWebSocketClient.normalize_url(url)

    async def start_reverse(self) -> None:
        await self._reverse.start()

    async def _reverse_ws_loop(self, url: str, entry: dict) -> None:
        await self._reverse.run_connection(url, entry)

    def start_heartbeat(self) -> None:
        self._connections.start_heartbeat()

    async def stop(self) -> None:
        await self._reverse.stop()
        await self._connections.stop()
        await self._forward.stop()

    async def broadcast(self, event: dict, appid: str = '') -> None:
        await self._connections.broadcast(event, appid)

    def status(self) -> dict[str, dict]:
        return {
            'forward': self._forward.status(),
            'reverse': self._reverse.status(),
        }

    async def handle_forward_ws(self, request: web.Request, entry: dict):
        return await self._forward.handle(request, entry)

    def _dispatch_message(self, ws: _WSWrapper, raw: str) -> None:
        self._connections.dispatch_message(ws, raw)

    async def _handle_message(self, ws: _WSWrapper, raw: str) -> None:
        await self._connections.handle_message(ws, raw)

    async def _heartbeat_loop(self) -> None:
        await self._connections.heartbeat_loop()
