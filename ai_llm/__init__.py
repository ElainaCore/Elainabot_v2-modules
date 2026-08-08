"""Public entry points for the shared AI service module."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import AIService


def get_service() -> AIService | None:
    """Return the running shared AI service, or ``None`` when disabled."""
    try:
        from core.application import get_app

        app = get_app()
        manager = getattr(app, 'module_manager', None)
        return manager.get('ai_llm') if manager else None
    except (AttributeError, RuntimeError):
        return None


__all__ = ['get_service']
