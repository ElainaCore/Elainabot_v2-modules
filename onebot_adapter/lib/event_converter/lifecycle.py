"""Elaina lifecycle event conversion."""

from __future__ import annotations

import time
from typing import Any

from modules.onebot_adapter.lib.group_join_request_flag import GroupJoinRequestFlagCodec


class LifecycleEventConverter:
    """Convert Elaina lifecycle events to OneBot notice/request events."""

    __slots__ = ()

    # (notice_type, sub_type, group_mode)
    # group_mode: robot=self joins/leaves; member=member joins/leaves; False=friend event.
    _EVENT_MAP = {
        'GROUP_ADD_ROBOT': ('group_increase', 'invite', 'robot'),
        'GROUP_DEL_ROBOT': ('group_decrease', 'kick_me', 'robot'),
        'GROUP_MEMBER_ADD': ('group_increase', 'approve', 'member'),
        'GROUP_MEMBER_REMOVE': ('group_decrease', 'leave', 'member'),
        'FRIEND_ADD': ('friend_add', '', False),
        'FRIEND_DEL': ('friend_recall', '', False),
    }

    async def convert_lifecycle_event(self, event: Any, id_mapper: Any, self_qq: int) -> dict[str, Any] | None:
        """Convert an Elaina lifecycle event to OneBot 11 format."""
        if event.event_type == 'GROUP_JOIN_REQUEST':
            return await self._convert_group_join_request(event, id_mapper, self_qq)

        entry = self._EVENT_MAP.get(event.event_type)
        if entry is None:
            return None

        notice_type, sub_type, group_mode = entry
        qq_user = await id_mapper.to_qq(event.user_id, 'user') if event.user_id else 0
        result = {
            'time': int(time.time()),
            'self_id': self_qq,
            'post_type': 'notice',
            'notice_type': notice_type,
            'real_user_id': event.user_id,
            'real_group_id': event.group_id,
            'user_id': self_qq if group_mode == 'robot' else qq_user,
        }
        if event_id := event.event_id:
            result['event_id'] = event_id
        if sub_type:
            result['sub_type'] = sub_type
        if group_mode:
            result['group_id'] = await id_mapper.to_qq(event.group_id, 'group') if event.group_id else 0
            result['operator_id'] = qq_user
        return result

    @staticmethod
    async def _convert_group_join_request(event: Any, id_mapper: Any, self_qq: int) -> dict[str, Any] | None:
        if not event.group_id or not event.user_id:
            return None

        qq_group = await id_mapper.to_qq(event.group_id, 'group')
        qq_user = await id_mapper.to_qq(event.user_id, 'user')
        verify_info = getattr(event, 'verify_info', None)
        if not isinstance(verify_info, dict):
            verify_info = event.get('d/verify_info') or {}
        comment = verify_info.get('verify_message', '') if isinstance(verify_info, dict) else ''
        if not comment and isinstance(verify_info, dict):
            qa_list = verify_info.get('review_qa_list')
            if isinstance(qa_list, list):
                comment = '\n'.join(
                    f'问：{item.get("question", "")}\n答：{item.get("answer", "")}'
                    for item in qa_list
                    if isinstance(item, dict) and (item.get('question') or item.get('answer'))
                )
        result = {
            'time': int(time.time()),
            'self_id': self_qq,
            'post_type': 'request',
            'request_type': 'group',
            'sub_type': 'add',
            'group_id': qq_group,
            'user_id': qq_user,
            'invitor_id': 0,
            'comment': str(comment or ''),
            'flag': GroupJoinRequestFlagCodec.encode(
                event.group_id,
                event.user_id,
                event.join_request_id,
            ),
            'event_id': event.event_id or '',
            'real_user_id': event.user_id,
            'real_group_id': event.group_id,
        }
        # Preserve QQ Bot group.add extension fields for OneBot consumers.
        for key in ('apply_at', 'apply_source', 'username', 'verify_method'):
            value = getattr(event, key, '')
            if value:
                result[key] = value
        qa_list = getattr(event, 'review_qa_list', None)
        if isinstance(qa_list, list):
            result['review_qa_list'] = [
                dict(item) for item in qa_list if isinstance(item, dict)
            ]
        if isinstance(verify_info, dict):
            result['verify_info'] = dict(verify_info)
        return result
