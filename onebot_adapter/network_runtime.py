"""OneBot 网络子系统的装配与生命周期管理。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from modules.onebot_adapter.action_context import ActionContext
from modules.onebot_adapter.config import OneBotConfig
from modules.onebot_adapter.lib.http_server import OneBotHTTPServer
from modules.onebot_adapter.lib.http_webhook import OneBotHTTPWebhook
from modules.onebot_adapter.lib.ws_server import OneBotWSServer


class OneBotNetworkRuntime:
    """根据配置创建、启动和关闭 OneBot 的四类网络连接。"""

    def __init__(
        self,
        *,
        action_context: ActionContext,
        on_action,
        framework_app: Callable[[], Any],
        framework_port: Callable[[], int],
        log: Any,
    ) -> None:
        self.action_context = action_context
        self.on_action = on_action
        self.framework_app = framework_app
        self.framework_port = framework_port
        self.log = log
        self.ws_server: OneBotWSServer | None = None
        self.http_server: OneBotHTTPServer | None = None
        self.http_webhook: OneBotHTTPWebhook | None = None

    async def start(self, config: OneBotConfig) -> None:
        port = self.framework_port()
        app = self.framework_app()

        forward_entries = self._forward_ws_entries(config)
        reverse_entries = self._reverse_ws_entries(config)
        self.ws_server = OneBotWSServer(
            heartbeat_interval=config.heartbeat_interval,
            on_action=self.on_action,
            default_qq=self.action_context.default_qq,
            qq_map=self.action_context.qq_map,
            log=self.log,
            forward_entries=forward_entries,
            reverse_entries=reverse_entries,
            debug=config.debug,
        )

        if app is not None:
            for path in self.ws_server.attach(app):
                suffix = ' (不校验路径)' if path == '/' else path
                self.log.info(f'正向 WS 已挂载: ws://0.0.0.0:{port}{suffix}')
        elif any(entry['port'] <= 0 for entry in forward_entries):
            self.log.warning('无法获取框架 aiohttp app, 正向 WS 未挂载')

        await self.ws_server.start_standalone()
        await self.ws_server.start_reverse()
        if reverse_entries:
            self.log.info(f'反向 WS 已启动: {len(reverse_entries)} 个连接')
        self.ws_server.start_heartbeat()

        http_entries = self._forward_http_entries(config)
        self.http_server = OneBotHTTPServer(
            entries=http_entries,
            on_action=self.on_action,
            log=self.log,
            debug=config.debug,
        )
        if app is not None:
            for path in self.http_server.attach(app):
                self.log.info(f'正向 HTTP 已挂载: http://0.0.0.0:{port}{path}')
        elif http_entries:
            self.log.warning('无法获取框架 aiohttp app, 正向 HTTP 未挂载')

        webhook_entries = self._webhook_entries(config)
        self.http_webhook = OneBotHTTPWebhook(
            entries=webhook_entries,
            on_action=self.on_action,
            log=self.log,
            debug=config.debug,
        )
        await self.http_webhook.start()
        if webhook_entries:
            self.log.info(f'HTTP 上报已启动: {len(webhook_entries)} 个目标')

    async def stop(self) -> None:
        if self.ws_server is not None:
            await self.ws_server.stop()
            self.ws_server = None
        if self.http_server is not None:
            self.http_server.detach()
            self.http_server = None
        if self.http_webhook is not None:
            await self.http_webhook.stop()
            self.http_webhook = None

    def status(self) -> dict[str, Any]:
        ws_status = self.ws_server.status() if self.ws_server else {'forward': {}, 'reverse': {}}
        return {
            'ws_server': ws_status.get('forward', {}),
            'ws_reverse': ws_status.get('reverse', {}),
            'http_server': self.http_server.status() if self.http_server else {},
            'http_webhook': self.http_webhook.status() if self.http_webhook else {},
            'port': self.framework_port(),
        }

    @staticmethod
    def _forward_ws_entries(config: OneBotConfig) -> list[dict]:
        return [
            {
                'name': connection['name'],
                'path': connection['path'],
                'port': int(connection.get('port', 0) or 0),
                'token': connection['access_token'],
                'appid': connection['appid'],
                'enable': True,
            }
            for connection in config.by_type('ws_server')
        ]

    @staticmethod
    def _reverse_ws_entries(config: OneBotConfig) -> list[dict]:
        return [
            {
                'name': connection['name'],
                'url': connection['url'],
                'appid': connection['appid'],
                'token': connection['access_token'],
                'reconnect_interval': connection['reconnect_interval'],
            }
            for connection in config.by_type('ws_reverse')
            if connection['url'].strip()
        ]

    @staticmethod
    def _forward_http_entries(config: OneBotConfig) -> list[dict]:
        return [
            {
                'name': connection['name'],
                'path': connection['path'],
                'token': connection['access_token'],
                'appid': connection['appid'],
                'enable': True,
            }
            for connection in config.by_type('http_server')
        ]

    @staticmethod
    def _webhook_entries(config: OneBotConfig) -> list[dict]:
        return [
            {
                'name': connection['name'],
                'url': connection['url'],
                'appid': connection['appid'],
                'token': connection['access_token'],
                'secret': connection['secret'],
                'timeout': connection['timeout'],
            }
            for connection in config.by_type('http_webhook')
            if connection['url'].strip()
        ]
