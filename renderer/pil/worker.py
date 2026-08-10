"""PIL 专用渲染 worker：只处理结构化的模块函数调用请求。"""

import contextlib
import ctypes
import gc
import importlib
from functools import lru_cache

_TRIM_EVERY = 16
_task_count = 0
_initialized = False

try:
    _libc = ctypes.CDLL('libc.so.6')
except OSError:
    _libc = None


@lru_cache(maxsize=128)
def _resolve_target(target_id):
    module_name, separator, function_name = target_id.rpartition(':')
    if not separator or not module_name.startswith('plugins.') or not function_name.isidentifier():
        raise ValueError('拒绝非插件模块或非模块级渲染函数')
    module = importlib.import_module(module_name)
    target = getattr(module, function_name)
    if (
        not callable(target)
        or getattr(target, '__module__', None) != module_name
        or getattr(target, '__name__', None) != function_name
    ):
        raise TypeError(f'{module_name}.{function_name} 不是模块级渲染函数')
    return target


def execute(request):
    """执行紧凑的 (target_id, args, kwargs) 渲染请求。"""
    global _initialized, _task_count
    if not _initialized:
        gc.disable()
        _initialized = True
    try:
        target_id, args, kwargs = request
        target = _resolve_target(target_id)
        return target(*args, **kwargs)
    finally:
        _task_count += 1
        if _task_count % _TRIM_EVERY == 0:
            with contextlib.suppress(Exception):
                gc.collect()
            if _libc is not None:
                with contextlib.suppress(Exception):
                    _libc.malloc_trim(0)
