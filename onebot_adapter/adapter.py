"""OneBot 适配器外观。

该类只协调配置、Hook、事件分发、网络运行时与 Action 路由。具体网络协议和
事件转换分别由 network_runtime.py、event_dispatcher.py 与 lib/ 下的组件负责。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from modules.onebot_adapter.action_context import ActionContext
from modules.onebot_adapter.action_registry import ActionRegistry
from modules.onebot_adapter.config import OneBotConfig
from modules.onebot_adapter.event_dispatcher import GroupNameResolver, OneBotEventDispatcher
from modules.onebot_adapter.hook_adapter import HookAdapter
from modules.onebot_adapter.lib.id_mapper import IDMapper
from modules.onebot_adapter.network_runtime import OneBotNetworkRuntime

if TYPE_CHECKING:
    from core.bot.manager import BotManager
    from core.message.event import Event
    from core.module.manager import ModuleContext
    from modules.onebot_adapter.lib.http_server import OneBotHTTPServer
    from modules.onebot_adapter.lib.http_webhook import OneBotHTTPWebhook
    from modules.onebot_adapter.lib.ws_server import OneBotWSServer


class OneBotAdapter:
    """协调 OneBot 子系统生命周期并保留模块的稳定入口。"""

    def __init__(self, module_ctx: ModuleContext) -> None:
        self._mctx = module_ctx
        self.log = module_ctx.log
        self.cfg = OneBotConfig()

        self.id_mapper: IDMapper | None = None
        self.ws_server: OneBotWSServer | None = None
        self.http_server: OneBotHTTPServer | None = None
        self.http_webhook: OneBotHTTPWebhook | None = None
        self._hook_adapter = HookAdapter(self.log)
        self._actx = ActionContext(log=self.log)
        self._action_registry: ActionRegistry | None = None
        self._bm: BotManager | None = None

        self._group_name_resolver = GroupNameResolver(self.log)
        # 保留旧属性，便于现有扩展检查或清空缓存。
        self._group_name_cache = self._group_name_resolver.cache
        self._group_name_locks = self._group_name_resolver.locks
        self._event_dispatcher = OneBotEventDispatcher(
            action_context=self._actx,
            id_mapper=self.id_mapper,
            group_names=self._group_name_resolver,
        )
        self._network_runtime = self._create_network_runtime()

    async def start(self) -> None:
        """加载配置和基础设施，然后安装 Hook 并启动网络连接。"""
        raw_config = self._mctx.read_config()
        migrated = OneBotConfig.migrate_legacy(raw_config)
        if migrated is not None:
            self._mctx.save_config(migrated, comments=OneBotConfig.comments())
            raw_config = migrated
            self.log.info('旧版配置已迁移为 connections 列表')
        else:
            raw_config = self._mctx.ensure_config(
                OneBotConfig.defaults(),
                comments=OneBotConfig.comments(),
            )
        self.cfg = OneBotConfig.from_dict(raw_config)
        self.log.info(f'配置: {len(self.cfg.connections)} 个连接')

        self.id_mapper = IDMapper(self._mctx.get_data_path('id_mapping.db'))
        await self.id_mapper.open()
        self.log.info('ID 映射数据库已加载')

        self._build_qq_map()
        self._actx.id_mapper = self.id_mapper
        self._event_dispatcher.id_mapper = self.id_mapper
        self._install_hooks()
        self._action_registry = ActionRegistry.create_default(self._actx)
        await self._start_network()

    async def stop(self) -> None:
        self._hook_adapter.uninstall()
        await self._stop_network()
        if self.id_mapper is not None:
            await self.id_mapper.close()
        self.log.info('OneBot 适配器已停止')

    async def apply_config(self, raw_config: dict) -> None:
        """保存新配置并只重启网络层。"""
        self.cfg = OneBotConfig.from_dict(raw_config)
        self._mctx.save_config(
            {
                'connections': self.cfg.connections,
                'heartbeat_interval': self.cfg.heartbeat_interval,
                'debug': self.cfg.debug,
            },
            comments=OneBotConfig.comments(),
        )
        await self._stop_network()
        await self._start_network()
        self.log.info('网络配置已重新应用')

    def network_status(self) -> dict[str, Any]:
        runtime = getattr(self, '_network_runtime', None)
        if runtime is not None:
            return runtime.status()
        ws_status = self.ws_server.status() if self.ws_server else {'forward': {}, 'reverse': {}}
        return {
            'ws_server': ws_status.get('forward', {}),
            'ws_reverse': ws_status.get('reverse', {}),
            'http_server': self.http_server.status() if self.http_server else {},
            'http_webhook': self.http_webhook.status() if self.http_webhook else {},
            'port': self._get_framework_port(),
        }

    def _build_qq_map(self) -> None:
        try:
            from core.base.config import cfg as framework_config

            for bot_config in framework_config.get_bot_configs() or []:
                appid = str(bot_config.get('appid', ''))
                robot_qq = bot_config.get('robot_qq', '')
                if appid and robot_qq:
                    self._actx.qq_map[appid] = int(robot_qq)
        except Exception as exc:
            self.log.debug(f'读取机器人 QQ 映射失败: {exc}')

        for appid, robot_qq in self._actx.qq_map.items():
            self.log.info(f'QQ 映射: appid={appid} → robot_qq={robot_qq}')
        self._actx.default_qq = next(iter(self._actx.qq_map.values()), 0)

    def _install_hooks(self) -> None:
        self._mctx.register_hook('on_raw_event', self._on_raw_event, priority=10)
        self._mctx.register_hook('after_send', self._on_after_send, priority=100)
        self._bm = self._get_bot_manager()
        if self._bm is not None:
            self._hook_adapter.install(self._bm)
            self._actx.bm = self._bm

    @staticmethod
    def _get_bot_manager() -> BotManager | None:
        try:
            from core.application import get_app

            return get_app()
        except Exception:
            return None

    @staticmethod
    def _get_framework_app():
        try:
            from core.application import get_app

            app = get_app()
            if app and app._http_server:
                return app._http_server._app
        except ImportError:
            pass
        return None

    @staticmethod
    def _get_framework_port() -> int:
        try:
            from core.base.config import cfg

            return cfg.get('settings', 'server.port', 5001)
        except Exception:
            return 5001

    def _create_network_runtime(self) -> OneBotNetworkRuntime:
        return OneBotNetworkRuntime(
            action_context=self._actx,
            on_action=self._handle_action,
            framework_app=self._get_framework_app,
            framework_port=self._get_framework_port,
            log=self.log,
        )

    async def _start_network(self) -> None:
        runtime = getattr(self, '_network_runtime', None)
        if runtime is None:
            runtime = self._create_network_runtime()
            self._network_runtime = runtime
        await runtime.start(self.cfg)
        self._sync_network_handles()

    async def _stop_network(self) -> None:
        runtime = getattr(self, '_network_runtime', None)
        if runtime is not None:
            await runtime.stop()
            self._sync_network_handles()
            return

        if self.ws_server is not None:
            await self.ws_server.stop()
            self.ws_server = None
        if self.http_server is not None:
            self.http_server.detach()
            self.http_server = None
        if self.http_webhook is not None:
            await self.http_webhook.stop()
            self.http_webhook = None

    def _sync_network_handles(self) -> None:
        self.ws_server = self._network_runtime.ws_server
        self.http_server = self._network_runtime.http_server
        self.http_webhook = self._network_runtime.http_webhook

    def _resolver(self) -> GroupNameResolver:
        resolver = getattr(self, '_group_name_resolver', None)
        cache = getattr(self, '_group_name_cache', None)
        locks = getattr(self, '_group_name_locks', None)
        if resolver is None or resolver.cache is not cache or resolver.locks is not locks:
            resolver = GroupNameResolver(self.log, cache=cache, locks=locks)
            self._group_name_resolver = resolver
            self._group_name_cache = resolver.cache
            self._group_name_locks = resolver.locks
        return resolver

    async def _get_cached_group_name(self, event: Event, bot: Any) -> str:
        """兼容旧调用入口，实际缓存行为由 GroupNameResolver 负责。"""
        return await self._resolver().resolve(event, bot)

    async def _on_raw_event(self, event: Event, bot: Any) -> None:
        dispatcher = getattr(self, '_event_dispatcher', None)
        resolver = self._resolver()
        if dispatcher is None or dispatcher.group_names is not resolver:
            dispatcher = OneBotEventDispatcher(
                action_context=self._actx,
                id_mapper=self.id_mapper,
                group_names=resolver,
            )
            self._event_dispatcher = dispatcher
        else:
            dispatcher.id_mapper = self.id_mapper
        await dispatcher.dispatch(
            event,
            bot,
            ws_server=self.ws_server,
            http_webhook=self.http_webhook,
        )

    async def _on_after_send(self, data: dict[str, Any]) -> None:
        """保留 after_send 扩展点。"""

    async def _handle_action(
        self,
        action: str,
        params: dict[str, Any],
        echo: str | None = None,
        appid: str = '',
    ) -> dict[str, Any]:
        return await self._action_registry.dispatch(action, params, echo, appid)
