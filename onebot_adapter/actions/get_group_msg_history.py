"""get_group_msg_history - 查询本地持久化的群消息历史。"""

from __future__ import annotations

from typing import Any

from modules.onebot_adapter.base_action import BaseAction
from modules.onebot_adapter.lib.group_history import GroupHistorySerializer


class GetGroupMessageHistoryAction(BaseAction):
    """以稳定的 message_seq 游标返回 OneBot 群消息。"""

    MAX_COUNT = 100
    _FETCH_FACTOR = 3

    async def execute(self, params: dict[str, Any], echo: str | None = None) -> dict[str, Any]:
        group_id = params.get('group_id', 0)
        count = self._positive_int(params.get('count', 20))
        message_seq = self._non_negative_int(params.get('message_seq', 0))
        appid, self_id = self._resolve_bot(params.get('self_id'))

        self._ctx.log.info(
            'get_group_msg_history 请求: '
            f'self_id={params.get("self_id") or self_id}, group_id={group_id}, '
            f'count={params.get("count", 20)}, message_seq={params.get("message_seq", 0)}'
        )
        if count is None or count > self.MAX_COUNT:
            return self._fail(f'count 必须为 1 到 {self.MAX_COUNT} 的整数', echo=echo)
        if message_seq is None:
            return self._fail('message_seq 必须为非负整数', echo=echo)
        if not appid or not self_id:
            return self._fail('缺少有效的 self_id', echo=echo)

        group_openid = await self._resolve_group_openid(group_id)
        if not group_openid:
            return self._fail('缺少有效的 group_id', echo=echo)
        id_mapper = self._ctx.id_mapper
        if not id_mapper:
            return self._fail('ID 映射器不可用', echo=echo)

        log_service = self._get_log_service(appid)
        if log_service is None:
            self._ctx.log.warning(
                f'get_group_msg_history 查询失败: self_id={self_id}, reason=日志服务不可用'
            )
            return self._fail('日志服务不可用', echo=echo)

        rows = await log_service.get_group_message_history(
            group_openid,
            count * self._FETCH_FACTOR,
            message_seq,
        )
        serializer = GroupHistorySerializer(id_mapper, appid, self_id)
        messages = await serializer.serialize(rows, count, group_id)
        self._ctx.log.info(
            f'get_group_msg_history 成功: self_id={self_id}, '
            f'group_id={group_openid}, count={len(messages)}'
        )
        return self._ok({'messages': messages}, echo=echo)

    def _resolve_bot(self, raw_self_id: Any) -> tuple[str, int]:
        requested = str(raw_self_id or '').strip()
        if requested:
            for appid, qq in self._ctx.qq_map.items():
                if str(qq) == requested:
                    return str(appid), int(qq)
            return '', 0

        appid = str(self._ctx.current_appid or '')
        if appid:
            qq = self._ctx.qq_map.get(appid, self._ctx.default_qq)
            if qq:
                return appid, int(qq)

        for candidate, qq in self._ctx.qq_map.items():
            if qq:
                return str(candidate), int(qq)
        return '', 0

    def _get_log_service(self, appid: str):
        service = self._ctx.log_services.get(appid)
        if service is not None:
            return service
        bots = getattr(self._ctx.bm, '_bots', {}) if self._ctx.bm else {}
        bot = bots.get(appid) if isinstance(bots, dict) else None
        service = getattr(bot, 'log_service', None)
        if service is not None:
            self._ctx.log_services[appid] = service
        return service

    async def _resolve_group_openid(self, raw_id: Any) -> str | None:
        value = str(raw_id or '').strip()
        if not value:
            return None
        if not value.isdigit():
            return value
        if not self._ctx.id_mapper:
            return None
        return await self._ctx.id_mapper.to_openid_by_type(int(value), 'group')

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            result = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return result if result > 0 else None

    @staticmethod
    def _non_negative_int(value: Any) -> int | None:
        try:
            result = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return result if result >= 0 else None
