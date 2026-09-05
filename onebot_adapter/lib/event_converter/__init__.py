"""Elaina Event -> OneBot 11 event conversion.

The package keeps the original module-level callables while exposing reusable
object-oriented conversion strategies.
"""

from modules.onebot_adapter.lib.event_converter.facade import EventConverter
from modules.onebot_adapter.lib.event_converter.lifecycle import LifecycleEventConverter
from modules.onebot_adapter.lib.event_converter.message import MessageEventConverter
from modules.onebot_adapter.lib.event_converter.segments import MessageSegmentConverter

__all__ = [
    'EventConverter',
    'LifecycleEventConverter',
    'MessageEventConverter',
    'MessageSegmentConverter',
    'convert_lifecycle_event',
    'convert_message_event',
    'event_converter',
]


# Stateless converters are allocated once so the event hot path only handles payload data.
event_converter = EventConverter()


# Backward-compatible module-level API. Direct aliases avoid an extra hot-path frame.
_build_segments = event_converter.message._from_event
_segments_to_raw = event_converter.message._to_raw
convert_message_event = event_converter.message.convert_message_event
convert_lifecycle_event = event_converter.lifecycle.convert_lifecycle_event
