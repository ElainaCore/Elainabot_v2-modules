"""统一消息发送服务 — Strategy 模式

封装发送路径的选择策略, 对应 sender.py 的全部 send 模式:
  - 含图片   → upload_media_bytes(type=1) → MSG_TYPE_MEDIA 发送
  - 含语音   → upload_media_bytes(type=3) → MSG_TYPE_MEDIA 发送
  - 含视频   → upload_media_bytes(type=2) → MSG_TYPE_MEDIA 发送
  - 含文件   → upload_media_bytes(type=4) 或 URL 直传 → MSG_TYPE_MEDIA 发送
  - 纯文本   → send_to_group / send_to_user
  - Markdown → MSG_TYPE_MARKDOWN 发送, 支持 buttons
  - Ark      → MSG_TYPE_ARK 发送, 参数语义与 reply_ark 一致
  - 按钮     → 通过 keyboard 参数传递
  - 回复引用 → message_reference 参数传递
"""

from __future__ import annotations

from typing import Any

from core.message._http import MessageType
from core.message.keyboard import build_ark
from core.message.sender import MessageSender
from modules.onebot_adapter.payload.segment_parser import ParsedMessage
from modules.onebot_adapter.payload.payload_converter import PayloadConverter


class MessageSenderService:
    """统一消息发送服务: 纯文本 / 图片 / 语音 / 视频 / 文件 / Markdown / Ark / 按钮"""

    extra_fields = {'msg_id', 'event_id'}
    extra_media_fields = {'msg_id', 'event_id'}

    @classmethod
    async def send(
        cls,
        sender: MessageSender,
        group_id: int | str | None,
        user_id: int | str | None,
        parsed: ParsedMessage,
        **kwargs,
    ) -> tuple[bool, Any, dict[str, Any]]:
        """统一发送入口 — 根据 ParsedMessage 选择策略
        kwargs: msg_id,event_id
        Returns:
            (ok, data, send_payload)
        """
        target = group_id or user_id
        prefix = 'groups' if group_id else 'users'

        # 1. 媒体文件 (语音/视频/文件) — 需要先上传再发送
        if parsed.media_type and parsed.media_type != 1:
            # voice=3, video=2, file=4
            payload = {x: kwargs[x] for x in cls.extra_media_fields if x in kwargs}
            return await cls._send_media(sender, target, prefix, parsed, group_id=group_id, user_id=user_id, **payload)

        # 2. 图片 — 上传后以 MSG_TYPE_MEDIA 发送
        if parsed.image_data:
            payload = {x: kwargs[x] for x in cls.extra_media_fields if x in kwargs}
            return await cls._send_media(sender, target, prefix, parsed, group_id=group_id, user_id=user_id, **payload)
        payload = {x: kwargs[x] for x in cls.extra_fields if x in kwargs}
        # 3. Markdown
        if parsed.msg_type == 'markdown' and parsed.markdown_content:
            return await cls._send_markdown(sender, group_id, user_id, target, parsed, **payload)

        # 4. Ark 卡片
        if parsed.msg_type == 'ark':
            return await cls._send_ark(sender, group_id, user_id, target, parsed, **payload)

        # 5. 纯文本 (可能带 buttons)
        return await cls._send_text(sender, group_id, user_id, target, parsed, **payload)

    # ==================== 文本发送 ====================

    @classmethod
    async def _send_text(
        cls,
        sender: MessageSender,
        group_id: int | str | None,
        user_id: int | str | None,
        target: int | str,
        parsed: ParsedMessage,
        **kwargs,
    ) -> tuple[bool, Any, dict[str, Any]]:
        content = parsed.text_content or '[空的文本消息]'
        if parsed.msg_type == 'raw_text':
            kwargs['msg_type'] = MessageType.MSG_TYPE_TEXT
        return await cls.send_msg_common(sender, group_id, user_id, target, parsed, content, **kwargs)

    # ==================== Markdown 发送 ====================

    @classmethod
    async def _send_markdown(
        cls,
        sender: MessageSender,
        group_id: int | str | None,
        user_id: int | str | None,
        target: int | str,
        parsed: ParsedMessage,
        **kwargs,
    ) -> tuple[bool, Any, dict[str, Any]]:
        content = parsed.markdown_content or parsed.text_content
        kwargs['msg_type'] = MessageType.MSG_TYPE_MARKDOWN
        return await cls.send_msg_common(sender, group_id, user_id, target, parsed, content, **kwargs)

    # ==================== Ark 发送 ====================

    @classmethod
    async def _send_ark(
        cls,
        sender: MessageSender,
        group_id: int | str | None,
        user_id: int | str | None,
        target: int | str,
        parsed: ParsedMessage,
        **kwargs,
    ) -> tuple[bool, Any, dict[str, Any]]:
        try:
            template_id, kv_data, content = cls._normalize_ark_call(parsed)
        except ValueError as exc:
            return False, {'message': str(exc), 'code': -1}, {}

        if parsed.message_reference:
            kwargs['message_reference'] = parsed.message_reference
        kwargs.update(
            {
                'content': content,
                'msg_type': MessageType.MSG_TYPE_ARK,
                'ark': build_ark(template_id, kv_data),
            }
        )
        func = sender.send_to_group if group_id else sender.send_to_user
        return await func(target, **kwargs)

    @staticmethod
    def _normalize_ark_call(parsed: ParsedMessage) -> tuple[Any, Any, Any]:
        """按 reply_ark(template_id, kv_data, content='') 绑定 JSON args/kwargs。"""
        if parsed.error:
            raise ValueError(parsed.error)

        args = list(parsed.ark_args or [])
        kwargs = dict(parsed.ark_kwargs or {})
        names = ('template_id', 'kv_data', 'content')
        if len(args) > len(names):
            raise ValueError('ark args 最多包含 template_id、kv_data、content 三项')

        unknown = set(kwargs) - set(names)
        if unknown:
            names_text = ', '.join(sorted(str(name) for name in unknown))
            raise ValueError(f'ark kwargs 包含不支持的参数: {names_text}')

        values = dict(zip(names, args, strict=False))
        for name, value in kwargs.items():
            if name in values:
                raise ValueError(f'ark 参数 {name} 被重复传入')
            values[name] = value

        missing = [name for name in names[:2] if name not in values]
        if missing:
            raise ValueError(f'ark 缺少必要参数: {", ".join(missing)}')
        return values['template_id'], values['kv_data'], values.get('content', '')

    @classmethod
    async def send_msg_common(
        cls,
        sender: MessageSender,
        group_id: int | str | None,
        user_id: int | str | None,
        target: int | str,
        parsed: ParsedMessage,
        content: str,
        **kwargs,
    ):
        kwargs |= PayloadConverter.convert(content)
        if parsed.buttons:
            kwargs['buttons'] = parsed.buttons
        if parsed.message_reference:
            kwargs['message_reference'] = parsed.message_reference
        func = sender.send_to_group if group_id else sender.send_to_user
        ok, data, send_payload = await func(target, **kwargs)
        return ok, data, send_payload

    # ==================== 媒体发送 ====================

    @classmethod
    async def _send_media(
        cls,
        sender: MessageSender,
        target: int | str,
        prefix: str,
        parsed: ParsedMessage,
        group_id: int | str | None = None,
        user_id: int | str | None = None,
        **kwargs,
    ) -> tuple[bool, Any, dict[str, Any]]:
        """统一媒体发送: image(1)/video(2)/voice(3)/file(4)"""
        media_type = parsed.media_type or 1
        media_data = parsed.media_data
        if not media_data:
            return False, '媒体数据为空', {}
        ctn = parsed.text_content
        data = await sender._send_media(
            sender,
            media_data,
            media_type,
            ctn,
            target_group_id=group_id,
            target_user_id=user_id,
            **kwargs,
        )
        error = sender.error if hasattr(sender, 'error') else None
        return data is not None, data or error, data
