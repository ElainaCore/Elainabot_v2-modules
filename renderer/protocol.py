"""Renderer-owned PIL rendering protocol."""

_pil_pool = None


def _bind_pil_pool(pool):
    """Bind the active pool during renderer module setup."""
    global _pil_pool
    _pil_pool = pool


async def render_pil(target, /, *args, **kwargs):
    """Render through the module-owned compact target-ID protocol."""
    pool = _pil_pool
    if pool is None or not pool.is_available():
        raise RuntimeError("PIL 渲染池不可用")
    return await pool.render(target, args, kwargs)
