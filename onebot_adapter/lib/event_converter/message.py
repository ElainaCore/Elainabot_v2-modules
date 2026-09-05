"""Elaina message event conversion."""

from __future__ import annotations

import time
from typing import Any

from modules.onebot_adapter.lib.event_converter.segments import MessageSegmentConverter


class MessageEventConverter:
    """Convert message-like Elaina events to OneBot events."""

    __slots__ = ('_from_event', '_to_raw')

    _SUPPORTED_EVENT_TYPES = frozenset(
        {
            'GROUP_AT_MESSAGE_CREATE',
            'GROUP_MESSAGE_CREATE',
            'C2C_MESSAGE_CREATE',
            'AT_MESSAGE_CREATE',
            'DIRECT_MESSAGE_CREATE',
            'MESSAGE_CREATE',
            'INTERACTION_CREATE',
        }
    )

    def __init__(self, segment_converter: MessageSegmentConverter) -> None:
        # Cache bound methods once: conversion does not allocate helper objects per event.
        self._from_event = segment_converter.from_event
        self._to_raw = segment_converter.to_raw

    async def convert_message_event(
        self,
        event: Any,
        id_mapper: Any,
        self_qq: int,
        *,
        group_name: str = '',
    ) -> dict[str, Any] | None:
        """Convert an Elaina message event to OneBot 11 format."""
        event_type = event.event_type
        if event_type not in self._SUPPORTED_EVENT_TYPES:
            return None

        user_id = event.user_id or ''
        if not user_id:
            return None

        group_id = event.group_id or ''
        qq_user = await id_mapper.to_qq(user_id, 'user')
        is_group = event.is_group or bool(group_id and event_type != 'C2C_MESSAGE_CREATE')
        qq_group = await id_mapper.to_qq(group_id, 'group') if is_group and group_id else 0

        segments = self._from_event(event)
        if isinstance(segments, dict):
            if is_group:
                return self._to_group_upload_event(event, self_qq, qq_user, qq_group, segments['data'])
            segments = [segments]

        if event_type == 'INTERACTION_CREATE' and getattr(event, 'interaction_data', None):
            self._prepend_button_segment(segments, event.interaction_data)

        now = int(time.time())
        message_id = hash(event.message_id or f'{now}{user_id}') & 0x7FFFFFFF
        result = {
            'time': now,
            'self_id': self_qq,
            'post_type': 'message',
            'message_type': 'group' if is_group else 'private',
            'sub_type': 'normal',
            'message_id': message_id,
            'raw_msg_id': event.message_id,
            'raw_ref_id': event.message_reference_id,
            'user_id': qq_user,
            'message': segments,
            'raw_message': self._to_raw(segments),
            'font': 0,
            'sender': {
                'user_id': qq_user,
                'nickname': getattr(event, 'username', '') or str(qq_user),
                'sex': 'unknown',
                'age': 0,
            },
            'real_user_id': event.user_id,
            'real_group_id': event.group_id,
            'full': event.is_full,
        }

        if is_group:
            result['group_id'] = qq_group
            result['group_name'] = str(group_name or '')
            result['sender']['card'] = ''
            result['sender']['role'] = event.member_role
            result['anonymous'] = None
        else:
            result['sub_type'] = 'friend'
        return result

    @staticmethod
    def _prepend_button_segment(segments: list[dict[str, Any]], interaction_data: dict[str, Any]) -> None:
        resolved = (interaction_data.get('data') or {}).get('resolved') or {}
        segments.insert(
            0,
            {
                'type': 'button',
                'data': {
                    'id': resolved.get('button_id', ''),
                    'data': resolved.get('button_data', ''),
                },
            },
        )

    @staticmethod
    def _to_group_upload_event(
        event: Any,
        self_qq: int,
        qq_user: int,
        qq_group: int,
        attachment: dict[str, Any],
    ) -> dict[str, Any]:
        file_data = dict(attachment)
        file_data.setdefault('id', attachment.get('file_id') or '')
        file_data.setdefault('name', attachment.get('filename') or attachment.get('file_name') or '')
        file_data.setdefault('size', attachment.get('file_size') or 0)
        file_data.setdefault('busid', 0)
        return {
            'time': int(time.time()),
            'self_id': self_qq,
            'post_type': 'notice',
            'notice_type': 'group_upload',
            'group_id': qq_group,
            'user_id': qq_user,
            'file': file_data,
            'real_user_id': event.user_id,
            'real_group_id': event.group_id,
        }
