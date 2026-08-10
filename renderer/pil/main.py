#!/usr/bin/env python
"""PIL 子进程渲染池子引擎

CPU 密集的 PIL 渲染放到独立子进程执行 (独立 GIL, 不卡主进程事件循环),
全局单例供所有插件共享。worker 使用 spawn 干净启动，进程池按需扩容、
空闲回收，崩溃自动重建。

插件应使用模块公开协议 modules.renderer.protocol.render_pil；本类只负责协议的
进程池实现。

配置: renderer 模块 data/pil.yaml
"""

import asyncio
import contextlib
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from core.base.logger import EXTENSION, get_logger
from modules.renderer.base import IdleEngine
from modules.renderer.pil.worker import execute

log = get_logger(EXTENSION, 'PIL渲染池')

_DEFAULTS = {
    'max_workers': 2,
    'max_concurrent': 4,
    'idle_timeout': 300,
    'task_timeout': 60,
    'max_tasks_per_worker': 300,
}

_COMMENTS = {
    'max_workers': '最大渲染子进程数 (按并发需求创建, 空闲自动回收)',
    'max_concurrent': '最大并发渲染任务数 (超出排队)',
    'idle_timeout': '整个渲染池空闲回收 (秒), 0=不回收',
    'task_timeout': '单次渲染超时 (秒)',
    'max_tasks_per_worker': '按 worker 数折算整池任务阈值, 空闲间隙重建释放内存, 0=不重建',
}


def _clean_process_context():
    """返回不会继承主框架堆内存的进程上下文。"""
    try:
        return 'spawn', multiprocessing.get_context('spawn')
    except ValueError as exc:
        raise RuntimeError('当前平台不支持 spawn，无法启动隔离的 PIL worker') from exc


def _make_request(target, args, kwargs):
    if not isinstance(target, str):
        raise TypeError('PIL 渲染目标必须是字符串')
    module, separator, function = target.rpartition(':')
    if (
        not separator
        or not module.startswith('plugins.')
        or not all(p.isidentifier() for p in module.split('.'))
    ):
        raise ValueError('PIL 渲染目标必须位于 plugins 下')
    if not function.isidentifier():
        raise ValueError('PIL 渲染目标必须指向模块级函数')
    if not isinstance(args, (list, tuple)) or not isinstance(kwargs, dict):
        raise TypeError('PIL 渲染参数类型无效')
    return target, tuple(args), kwargs


class PILRenderPool(IdleEngine):
    """按需扩容、空闲回收并自动轮换 worker 的 PIL 渲染池。"""

    __slots__ = ('_pool', '_pool_tasks', '_lifecycle_lock', '_start_method', '_mp_context')

    def __init__(self, cfg):
        super().__init__(cfg, cfg.get('max_concurrent', 4))
        self._pool = None
        self._pool_tasks = 0
        self._start_method, self._mp_context = _clean_process_context()
        # 用同一把锁保护任务进入和进程池回收，避免回收竞态。
        self._lifecycle_lock = asyncio.Lock()

    async def render(self, target, args=(), kwargs=None):
        """提交目标 ID 和数据，不向 worker 传递 Python callable。"""
        if self._closed:
            raise RuntimeError('PIL 渲染池已关闭')
        request = _make_request(target, args, kwargs or {})
        async with self._semaphore:
            # 等待旧池摘除后再创建新池。
            async with self._lifecycle_lock:
                self._active += 1
            try:
                return await self._run_in_pool(request, retried=False)
            finally:
                async with self._lifecycle_lock:
                    self._active -= 1
                    self._mark_released()
                    await self._maybe_recycle_pool()

    async def _run_in_pool(self, request, retried):
        pool = await self._ensure_pool()
        loop = asyncio.get_running_loop()
        timeout = self._cfg.get('task_timeout', 60)
        try:
            fut = loop.run_in_executor(pool, execute, request)
            result = await asyncio.wait_for(fut, timeout=timeout)
            self._pool_tasks += 1
            return result
        except TimeoutError:
            # 卡住的进程无法随 future 取消，直接回收整个池。
            fut.cancel()
            await self._discard(pool, force=True)
            log.warning(f'渲染任务 {request[0]} 超时({timeout}s), 已回收渲染进程池')
            raise
        except BrokenProcessPool as e:
            await self._discard(pool, force=True)
            if retried:
                raise RuntimeError('PIL 渲染进程池连续崩溃') from e
            log.warning('渲染进程池崩溃, 重建后重试')
            return await self._run_in_pool(request, retried=True)

    async def _ensure_pool(self):
        pool = self._pool
        if pool is not None:
            return pool
        async with self._lock:
            pool = self._pool
            if pool is not None:
                return pool
            workers = max(1, int(self._cfg.get('max_workers', 2)))
            task_limit = max(0, int(self._cfg.get('max_tasks_per_worker', 300)))
            pool = ProcessPoolExecutor(max_workers=workers, mp_context=self._mp_context)
            self._pool = pool
            self._pool_tasks = 0
            if self._cfg.get('idle_timeout', 0):
                self._start_idle_cleanup(60)
            log.info(
                f'PIL 渲染进程池已创建 ({workers} worker, {self._start_method}, '
                f'回收阈值 {task_limit * workers if task_limit else "不限"} 任务)'
            )
            return pool

    @property
    def start_method(self):
        return self._start_method

    async def _discard(self, pool, force=False):
        async with self._lock:
            if pool is not self._pool:
                return
            self._pool = None
        processes = list((getattr(pool, '_processes', None) or {}).values())
        if force:
            for process in processes:
                with contextlib.suppress(Exception):
                    process.kill()
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)

    async def _maybe_recycle_pool(self):
        per_worker = max(0, int(self._cfg.get('max_tasks_per_worker', 300)))
        workers = max(1, int(self._cfg.get('max_workers', 2)))
        if (
            per_worker
            and self._active == 0
            and self._pool is not None
            and self._pool_tasks >= per_worker * workers
        ):
            pool = self._pool
            completed = self._pool_tasks
            await self._discard(pool)
            log.info(f'PIL 渲染进程池累计 {completed} 个任务后回收')

    async def _release_idle(self):
        """整个进程池空闲超时后回收。"""
        async with self._lifecycle_lock:
            # 再次检查活动任务，避免调度间隙误杀新任务。
            if self._active != 0:
                return
            timeout = self._cfg.get('idle_timeout', 300)
            if self._pool is not None and time.monotonic() - self._last_release >= timeout:
                pool = self._pool
                await self._discard(pool)
                log.info('PIL 渲染进程池空闲回收')

    async def close(self):
        self._closed = True
        self._stop_idle_cleanup()
        if self._pool is not None:
            await self._discard(self._pool, force=True)
