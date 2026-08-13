"""get_group_info - query and return real group information."""

from __future__ import annotations

from typing import Any

from modules.onebot_adapter.base_action import BaseAction


class GetGroupInfoAction(BaseAction):
    """Convert QQ group information to the OneBot 11 response shape."""

    async def execute(self, params: dict[str, Any], echo: str | None = None) -> dict[str, Any]:
        group_id = params.get('group_id', 0)
        self._ctx.log.info(f'get_group_info 请求: group_id={group_id}')

        group_openid = await self._resolve_group_openid(group_id)
        if not group_openid:
            self._ctx.log.warning(f'get_group_info 参数错误: 无效 group_id={group_id}')
            return self._fail('缺少有效的 group_id', echo=echo)

        sender = self._ctx.get_sender()
        if not sender:
            self._ctx.log.warning(
                f'get_group_info 查询失败: group_id={group_openid}, '
                'reason=无可用的消息发送器'
            )
            return self._fail('无可用的消息发送器', echo=echo)

        group, error = await sender.get_group_info(group_openid, return_error=True)
        if group is None:
            self._ctx.log.warning(
                f'get_group_info 查询失败: group_id={group_openid}, response={error}'
            )
            return self._platform_fail(error, echo)

        member_count = self._to_non_negative_int(
            group.get('group_member_num', group.get('member_count', 0))
        )
        max_member_count = self._to_non_negative_int(
            group.get('max_group_member_num', group.get('max_member_count', 0))
        )
        result = {
            'group_id': group_id,
            'group_name': str(group.get('group_name') or ''),
            'member_count': member_count,
            'max_member_count': max_member_count,
        }
        self._ctx.log.info(
            f'get_group_info 成功: group_id={group_openid}, member_count={member_count}'
        )
        return self._ok(result, echo=echo)

    async def _resolve_group_openid(self, raw_id: Any) -> str | None:
        value = str(raw_id or '').strip()
        if not value:
            return None
        if not value.isdigit():
            return value
        if not self._ctx.id_mapper:
            return None
        return await self._ctx.id_mapper.to_openid_by_type(int(value), 'group')

    def _platform_fail(self, response: Any, echo: str | None) -> dict[str, Any]:
        data = response if isinstance(response, dict) else {}
        message = str(data.get('message') or data.get('msg') or response or '查询群信息失败')
        try:
            retcode = int(data.get('code') or data.get('err_code') or 1)
        except (TypeError, ValueError):
            retcode = 1
        return self._fail(message, echo=echo, retcode=retcode)

    @staticmethod
    def _to_non_negative_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0
