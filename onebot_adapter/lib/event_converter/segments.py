"""OneBot message segment construction and CQ serialization."""

from __future__ import annotations

import html
import re
from typing import Any

_URL_IN_ANGLE = re.compile(r'<(https?://[^>]+)>')


class MessageSegmentConverter:
    """Convert Event message content to OneBot segments and CQ text."""

    __slots__ = ()

    @staticmethod
    def from_event(event: Any) -> list[dict[str, Any]] | dict[str, Any]:
        """Build OneBot segments, or return a file attachment marker."""
        segments: list[dict[str, Any]] = []

        for attachment in getattr(event, 'attachments', None) or ():
            if not isinstance(attachment, dict):
                continue
            content_type = attachment.get('content_type', '')
            url = html.unescape(attachment.get('url', '') or '')
            if not url:
                continue
            if content_type.startswith('image/'):
                segments.append({'type': 'image', 'data': {'file': url, 'url': url}})
            elif content_type.startswith('audio/') or content_type.startswith('voice/'):
                segments.append({'type': 'record', 'data': {'file': url, 'url': url}})
            elif content_type.startswith('video/'):
                segments.append({'type': 'video', 'data': {'file': url, 'url': url}})
            elif content_type == 'file':
                return {'type': 'file', 'data': attachment}

        text = getattr(event, 'content', '') or ''
        if text:
            text = _URL_IN_ANGLE.sub('', text).strip()
        if text:
            segments.insert(0, {'type': 'text', 'data': {'text': text}})

        if not segments:
            segments.append({'type': 'text', 'data': {'text': ' '}})

        if quoted_message := getattr(event, 'msg_elements', ''):
            segments.append({'type': 'reply', 'data': {'content': quoted_message}})

        for user in getattr(event, 'mentions', None) or ():
            scope = user.get('scope') or 'single'
            user_id = 0 if scope == 'all' else user.get('id') or user.get('member_openid')
            segments.append({'type': 'at', 'data': {'qq': user_id} | user})
        return segments

    @staticmethod
    def to_raw(segments: list[dict[str, Any]]) -> str:
        """Serialize OneBot segments to a raw CQ-code string."""
        parts: list[str] = []
        for segment in segments:
            segment_type = segment.get('type', '')
            data = segment.get('data', {})
            if segment_type == 'text':
                parts.append(data.get('text', ''))
            elif segment_type == 'image':
                parts.append(f'[CQ:image,file={data.get("file", "")}]')
            elif segment_type == 'record':
                parts.append(f'[CQ:record,file={data.get("file", "")}]')
            elif segment_type == 'video':
                parts.append(f'[CQ:video,file={data.get("file", "")}]')
            elif segment_type == 'reply':
                parts.append('[CQ:reply]')
            else:
                fields = ','.join(f'{key}={value}' for key, value in data.items())
                parts.append(f'[CQ:{segment_type},{fields}]' if fields else f'[CQ:{segment_type}]')
        return ''.join(parts)
