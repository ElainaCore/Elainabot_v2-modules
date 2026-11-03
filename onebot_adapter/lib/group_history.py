"""将持久化消息日志转换为 OneBot 群聊历史。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from core.message.event import Event
from modules.onebot_adapter.lib.event_converter import convert_message_event


class GroupHistorySerializer:
    """负责历史消息去重、排序及 OneBot 格式转换。"""

    def __init__(self, id_mapper: Any, appid: str, self_id: int) -> None:
        self._id_mapper = id_mapper
        self._appid = appid
        self._self_id = self_id

    async def serialize(
        self,
        rows: list[dict[str, Any]],
        count: int,
        group_id: Any,
    ) -> list[dict[str, Any]]:
        selected = self._deduplicate(rows)[:count]
        messages = []
        for row in reversed(selected):
            messages.append(await self._serialize_row(row, group_id))
        return messages

    @staticmethod
    def _deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """发送链路可能写入两次相同 message_id，优先保留较新的记录。"""
        result = []
        seen = set()
        for row in rows:
            message_id = str(row.get('message_id') or '')
            key = (str(row.get('direction') or ''), message_id) if message_id else None
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            result.append(row)
        return result

    async def _serialize_row(
        self,
        row: dict[str, Any],
        group_id: Any,
    ) -> dict[str, Any]:
        if row.get('direction') != 'send':
            message = await self._serialize_received(row, group_id)
            if message is not None:
                return message
        return await self._serialize_log_row(row, group_id)

    async def _serialize_received(
        self,
        row: dict[str, Any],
        group_id: Any,
    ) -> dict[str, Any] | None:
        raw = self._load_json(row.get('raw_message'))
        if not isinstance(raw, dict):
            return None
        try:
            event = Event.from_websocket(self._appid, raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not event.is_group or not event.user_id:
            return None

        message = await convert_message_event(event, self._id_mapper, self._self_id)
        if message is None:
            return None
        message['time'] = self._unix_time(event.timestamp or row.get('timestamp'))
        message['message_seq'] = int(row['message_seq'])
        message['group_id'] = group_id
        message['raw_ref_id'] = str(row.get('reference_id') or message.get('raw_ref_id') or '')
        reply_id = self._extract_reply_id(raw)
        if reply_id:
            segments = message.get('message') if isinstance(message.get('message'), list) else []
            message['message'] = [
                {'type': 'reply', 'data': {'id': reply_id}},
                *(
                    segment
                    for segment in segments
                    if not isinstance(segment, dict) or segment.get('type') != 'reply'
                ),
            ]
            raw_message = str(message.get('raw_message') or '').replace('[CQ:reply]', '')
            message['raw_message'] = f'[CQ:reply,id={reply_id}]{raw_message}'
        message['openid'] = str(event.user_id or '')
        message['raw_json'] = raw
        return message

    async def _serialize_log_row(
        self,
        row: dict[str, Any],
        group_id: Any,
    ) -> dict[str, Any]:
        is_send = row.get('direction') == 'send'
        if is_send:
            user_id = self._self_id
            nickname = 'ElainaBot'
        else:
            real_user_id = str(row.get('user_id') or '')
            user_id = await self._id_mapper.to_qq(real_user_id, 'user') if real_user_id else 0
            nickname = str(user_id)

        raw_json = self._load_json(row.get('raw_message'))
        content = str(row.get('content') or '')
        segments = [{'type': 'text', 'data': {'text': content or ' '}}]
        reply_id = self._extract_reply_id(raw_json)
        if reply_id:
            segments.insert(0, {'type': 'reply', 'data': {'id': reply_id}})

        message_id = row.get('message_id') or (int(row['message_seq']) & 0x7FFFFFFF)
        return {
            'time': self._unix_time(row.get('timestamp')),
            'self_id': self._self_id,
            'post_type': 'message',
            'message_type': 'group',
            'sub_type': 'normal',
            'message_id': message_id,
            'message_seq': int(row['message_seq']),
            'user_id': user_id,
            'group_id': group_id,
            'message': segments,
            'raw_message': content,
            'font': 0,
            'sender': {
                'user_id': user_id,
                'nickname': nickname,
                'card': '',
                'sex': 'unknown',
                'age': 0,
                'role': 'member',
            },
            'raw_ref_id': str(row.get('reference_id') or ''),
            'real_user_id': '' if is_send else str(row.get('user_id') or ''),
            'real_group_id': str(row.get('group_id') or ''),
            'openid': '' if is_send else real_user_id,
            'raw_json': raw_json,
        }

    @classmethod
    def _extract_reply_id(cls, raw_message: Any) -> str:
        payload = cls._load_json(raw_message)
        if not isinstance(payload, dict):
            return ''
        reference = payload.get('message_reference')
        if isinstance(reference, dict):
            return str(reference.get('message_id') or reference.get('id') or '')
        event_data = payload.get('d')
        elements = event_data.get('msg_elements') if isinstance(event_data, dict) else None
        for element in elements if isinstance(elements, list) else []:
            if isinstance(element, dict) and element.get('message_type') == 103:
                return str(element.get('msg_idx') or '')
        return ''

    @staticmethod
    def _load_json(value: Any) -> Any:
        if isinstance(value, dict | list):
            return value
        try:
            return json.loads(str(value or ''))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _unix_time(value: Any) -> int:
        if isinstance(value, int | float):
            return max(0, int(value))
        text = str(value or '').strip()
        if not text:
            return 0
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
            return max(0, int(parsed.timestamp()))
        except (OverflowError, ValueError):
            return 0
