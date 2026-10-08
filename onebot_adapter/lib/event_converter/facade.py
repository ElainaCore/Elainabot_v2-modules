"""Facade composing the OneBot event conversion strategies."""

from modules.onebot_adapter.lib.event_converter.lifecycle import LifecycleEventConverter
from modules.onebot_adapter.lib.event_converter.message import MessageEventConverter
from modules.onebot_adapter.lib.event_converter.segments import MessageSegmentConverter


class EventConverter:
    """Expose reusable message and lifecycle conversion strategies."""

    __slots__ = ('message', 'lifecycle')

    def __init__(self) -> None:
        segment_converter = MessageSegmentConverter()
        self.message = MessageEventConverter(segment_converter)
        self.lifecycle = LifecycleEventConverter()
