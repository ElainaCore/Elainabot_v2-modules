"""ElainaBot shared AI service module."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os

import aiohttp
from aiohttp import web

from core.base.config import cfg
from core.base.logger import EXTENSION, get_logger
from core.plugin.web_pages import register_page, register_route, unregister_page, unregister_route

from .migration import load_ai_dev_config
from .service import DEFAULT_CONFIG, AIService

__module_meta__ = {
    'name': 'AI LLM 服务',
    'description': '统一管理 LLM、Agent、MCP、Skills、沙箱与计划任务',
    'version': '1.0.0',
    'author': 'ElainaBot',
}

log = get_logger(EXTENSION, 'AI LLM服务')
PREFIX = '/api/ext/ai-service'
PAGE_KEY = 'ai-service'
_instance: AIService | None = None
_ctx = None
_refresh_task: asyncio.Task | None = None
_ASSET_FILES = {
    'panel.css': 'text/css',
    'core.js': 'text/javascript',
    'providers.js': 'text/javascript',
    'app.js': 'text/javascript',
    'logs.js': 'text/javascript',
}


async def setup(ctx):
    global _instance, _ctx, _refresh_task
    _ctx = ctx
    existing = ctx.read_config()
    defaults = DEFAULT_CONFIG
    if not existing.get('providers'):
        bot_root = os.path.dirname(os.path.dirname(ctx.module_dir))
        migrated = load_ai_dev_config(
            bot_root,
            settings_get=lambda key, default: cfg.get('settings', f'ai.{key}', default),
        )
        if migrated:
            defaults = migrated
            log.info('已从 AI 开发工具迁移接口配置')
    config = ctx.ensure_config(defaults)

    def save(value):
        ctx.save_config(value)

    _instance = AIService(config, save, ctx.data_dir)
    await _instance.runtime.start(ctx.emit)
    register_route('GET', f'{PREFIX}/config', _get_config)
    register_route('PUT', f'{PREFIX}/config', _save_config)
    register_route('POST', f'{PREFIX}/models', _models)
    register_route('POST', f'{PREFIX}/health', _health)
    register_route('POST', f'{PREFIX}/refresh-all', _refresh_all)
    register_route('POST', f'{PREFIX}/test', _test)
    register_route('GET', f'{PREFIX}/runtime', _runtime)
    register_route('GET', f'{PREFIX}/skills', _skills)
    register_route('PUT', f'{PREFIX}/plugin-capabilities', _save_plugin_capabilities)
    register_route('POST', f'{PREFIX}/mcp/refresh', _mcp_refresh)
    register_route('POST', f'{PREFIX}/interrupt', _interrupt)
    register_route('GET', f'{PREFIX}/logs', _logs)
    register_route('DELETE', f'{PREFIX}/logs', _clear_logs)
    for filename in _ASSET_FILES:
        register_route('GET', f'{PREFIX}/assets/{filename}', _asset, auth=False)
    register_page(
        key=PAGE_KEY,
        label='AI LLM 服务',
        source='module',
        source_name='AI LLM 服务',
        html_file=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'panel.html'),
    )
    if config.get('auto_fetch_models'):
        _refresh_task = asyncio.create_task(_refresh_missing_models())
    log.info('AI LLM 服务已启动')
    return _instance


async def teardown():
    global _instance, _ctx, _refresh_task
    if _refresh_task and not _refresh_task.done():
        _refresh_task.cancel()
    _refresh_task = None
    if _instance is not None:
        await _instance.runtime.stop()
    for method, path in (
        ('GET', f'{PREFIX}/config'),
        ('PUT', f'{PREFIX}/config'),
        ('POST', f'{PREFIX}/models'),
        ('POST', f'{PREFIX}/health'),
        ('POST', f'{PREFIX}/refresh-all'),
        ('POST', f'{PREFIX}/test'),
        ('GET', f'{PREFIX}/runtime'),
        ('GET', f'{PREFIX}/skills'),
        ('PUT', f'{PREFIX}/plugin-capabilities'),
        ('POST', f'{PREFIX}/mcp/refresh'),
        ('POST', f'{PREFIX}/interrupt'),
        ('GET', f'{PREFIX}/logs'),
        ('DELETE', f'{PREFIX}/logs'),
    ):
        unregister_route(method, path)
    for filename in _ASSET_FILES:
        unregister_route('GET', f'{PREFIX}/assets/{filename}')
    unregister_page(PAGE_KEY)
    _instance = None
    _ctx = None


def _service() -> AIService:
    if _instance is None:
        raise web.HTTPServiceUnavailable(text='AI service is not running')
    return _instance


def get_service() -> AIService | None:
    """Public plugin API that remains valid under the dynamic module loader."""
    return _instance


async def _json(request: web.Request) -> dict:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


async def _asset(request: web.Request):
    filename = request.path.rsplit('/', 1)[-1]
    content_type = _ASSET_FILES.get(filename)
    if not content_type:
        raise web.HTTPNotFound()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', filename)
    if not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={
        'Cache-Control': 'no-store',
        'Content-Type': content_type,
    })


async def _get_config(_request):
    return web.json_response({'success': True, 'data': _service().config(public=True)})


async def _save_config(request):
    try:
        data = await _service().save(await _json(request))
        if data.get('auto_fetch_models'):
            await _refresh_models()
            data = _service().config(public=True)
        return web.json_response({'success': True, 'data': data})
    except (TypeError, ValueError) as error:
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _models(request):
    body = await _json(request)
    try:
        models = await _service().fetch_models(str(body.get('provider_id') or ''))
        return web.json_response({'success': True, 'data': {'models': models}})
    except (RuntimeError, OSError) as error:
        return web.json_response({'success': False, 'error': str(error)}, status=502)


async def _health(request):
    body = await _json(request)
    models = body.get('models')
    if models is not None and not isinstance(models, list):
        return web.json_response({'success': False, 'error': 'models 必须是数组'}, status=400)
    try:
        results = await _service().probe_models(
            str(body.get('provider_id') or ''), models,
        )
        return web.json_response({'success': True, 'data': {'results': results}})
    except (RuntimeError, OSError) as error:
        return web.json_response({'success': False, 'error': str(error)}, status=502)


async def _refresh_all(_request):
    result = await _refresh_models(force=True)
    return web.json_response({'success': True, 'data': result})


async def _test(request):
    body = await _json(request)
    response = web.StreamResponse(headers={
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache, no-transform',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
    })
    await response.prepare(request)
    try:
        async for event in _service().stream_complete(
            [{'role': 'user', 'content': str(body.get('message') or '你好，请简短回复。')}],
            provider_id=str(body.get('provider_id') or ''),
            model=str(body.get('model') or ''),
            max_tokens=200,
            runtime_prompt=str(body.get('runtime_prompt') or ''),
            session_id='web:ai-service-test',
        ):
            payload = json.dumps({'success': True, 'data': event}, ensure_ascii=False)
            await response.write(f'data: {payload}\n\n'.encode('utf-8'))
    except (RuntimeError, OSError, aiohttp.ClientError) as error:
        payload = json.dumps({
            'success': False,
            'data': {'type': 'error', 'error': str(error)[:500]},
        }, ensure_ascii=False)
        with contextlib.suppress(ConnectionResetError, RuntimeError):
            await response.write(f'data: {payload}\n\n'.encode('utf-8'))
    finally:
        with contextlib.suppress(ConnectionResetError, RuntimeError):
            await response.write_eof()
    return response


async def _runtime(_request):
    return web.json_response({'success': True, 'data': _service().runtime.status()})


async def _skills(_request):
    return web.json_response({'success': True, 'data': _service().runtime.skills()})


async def _save_plugin_capabilities(request):
    body = await _json(request)
    items = body.get('items')
    if not isinstance(items, list):
        return web.json_response({'success': False, 'error': 'items 必须是数组'}, status=400)
    try:
        data = await _service().save_plugin_capabilities(items)
        return web.json_response({'success': True, 'data': {'items': data}})
    except (TypeError, ValueError) as error:
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _mcp_refresh(_request):
    try:
        tools = await _service().runtime.refresh_mcp_tools()
        return web.json_response({'success': True, 'data': {'tools': tools}})
    except (OSError, ValueError, RuntimeError) as error:
        return web.json_response({'success': False, 'error': str(error)}, status=502)


async def _interrupt(request):
    body = await _json(request)
    target = str(body.get('run_id') or body.get('session_id') or '').strip()
    if not target:
        return web.json_response({'success': False, 'error': '缺少 run_id 或 session_id'}, status=400)
    stopped = _service().runtime.interrupt(target)
    return web.json_response({'success': True, 'data': {'interrupted': stopped}})


async def _logs(request):
    run_id = str(request.query.get('run_id') or '').strip()
    if run_id:
        value = _service().audit.get(run_id)
        if value is None:
            return web.json_response({'success': False, 'error': '调用日志不存在'}, status=404)
        return web.json_response({'success': True, 'data': value})
    try:
        limit = int(request.query.get('limit') or 100)
    except ValueError:
        limit = 100
    values = _service().audit.list(
        limit=limit,
        status=str(request.query.get('status') or ''),
        provider=str(request.query.get('provider') or ''),
        search=str(request.query.get('search') or ''),
    )
    return web.json_response({
        'success': True,
        'data': {'items': values, 'stats': _service().audit.stats()},
    })


async def _clear_logs(_request):
    _service().audit.clear()
    return web.json_response({'success': True, 'data': {'cleared': True}})


async def _refresh_models(force: bool = False) -> dict:
    service = _service()
    providers = service.config().get('providers', [])
    refreshed, failed = {}, {}
    for provider in providers:
        if not provider.get('enabled') or not provider.get('api_key'):
            continue
        if not force and len(provider.get('models', [])) > 1:
            continue
        try:
            refreshed[provider['id']] = len(await service.fetch_models(provider['id']))
        except Exception as error:
            failed[provider['id']] = str(error)[:160]
    return {'refreshed': refreshed, 'failed': failed}


async def _refresh_missing_models():
    await asyncio.sleep(1)
    try:
        result = await _refresh_models()
        if result['refreshed']:
            log.info('已自动刷新模型列表: %s', result['refreshed'])
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log.warning('自动刷新模型列表失败: %s', error)
