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
        if manager is None:
            return None
        service = manager.get('ai_llm')
        if service is not None:
            return service
        for item in manager.list_modules():
            if str(item.get('display_name') or '').strip() == 'AI LLM 服务':
                return manager.get(str(item.get('name') or ''))
        return None
    except (AttributeError, RuntimeError):
        return None


__all__ = ['get_service']
