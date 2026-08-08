"""Shared OpenAI-compatible LLM service for ElainaBot plugins."""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import aiohttp

from .runtime import AgentRuntime

ToolHandler = Callable[[str, dict], Awaitable[dict] | dict]


class AIServiceError(RuntimeError):
    pass


DEFAULT_CONFIG = {
    'enabled': True,
    'active_provider': 'ytea',
    'auto_switch': True,
    'auto_fetch_models': True,
    'request_timeout': 120,
    'temperature': 0.7,
    'max_tokens': 8192,
    'max_tool_rounds': 6,
    'agent_enabled': True,
    'runtime_prompt': '',
    'context': {
        'max_tokens': 65536,
        'max_turns': 30,
        'compress_enabled': True,
        'keep_recent_ratio': 0.25,
    },
    'skills': {'enabled': True, 'enabled_ids': []},
    'mcp': {'enabled': False, 'servers': []},
    'sandbox': {
        'enabled': False,
        'endpoint': '',
        'token': '',
        'timeout': 30,
        'execution_timeout': 20,
    },
    'subagents': [],
    'cron_jobs': [],
    'providers': [
        {
            'id': 'ytea',
            'name': 'YTea',
            'base_url': 'https://api.ytea.top/v1',
            'api_key': '',
            'model': 'gpt-4.1-nano',
            'models': ['gpt-4.1-nano'],
            'model_priority': ['gpt-4.1-nano'],
            'disabled_models': [],
            'model_priority_enabled': True,
            'priority': 100,
            'enabled': True,
            'builtin': True,
        }
    ],
}


def normalize_config(value: dict | None) -> dict:
    result = copy.deepcopy(DEFAULT_CONFIG)
    if isinstance(value, dict):
        for key in result:
            if key in value:
                result[key] = copy.deepcopy(value[key])
    providers = result.get('providers')
    if not isinstance(providers, list) or not providers:
        providers = copy.deepcopy(DEFAULT_CONFIG['providers'])
    seen = set()
    normalized = []
    for raw in providers:
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        item['id'] = str(item.get('id') or uuid.uuid4().hex[:8]).strip()
        if not item['id'] or item['id'] in seen:
            continue
        seen.add(item['id'])
        item['name'] = str(item.get('name') or item['id']).strip()
        item['base_url'] = str(item.get('base_url') or '').strip().rstrip('/')
        item['api_key'] = str(item.get('api_key') or '').strip()
        item['model'] = str(item.get('model') or '').strip()
        item['enabled'] = bool(item.get('enabled', True))
        item['builtin'] = bool(item.get('builtin', False))
        item['priority'] = min(10000, max(0, int(item.get('priority', 100))))
        item['model_priority_enabled'] = bool(item.get('model_priority_enabled', True))
        models = item.get('models', [])
        if not isinstance(models, list):
            models = []
        models = list(dict.fromkeys(str(model).strip() for model in models if str(model).strip()))[:1000]
        if item['model'] and item['model'] not in models:
            models.insert(0, item['model'])
        if not item['model'] and models:
            item['model'] = models[0]
        priority = item.get('model_priority', [])
        if not isinstance(priority, list):
            priority = []
        priority = list(dict.fromkeys(str(model).strip() for model in priority if str(model).strip()))
        priority.extend(model for model in models if model not in priority)
        disabled = item.get('disabled_models', [])
        if not isinstance(disabled, list):
            disabled = []
        item['disabled_models'] = [str(model) for model in disabled if str(model) in models]
        item['models'] = models
        item['model_priority'] = priority[:1000]
        saved_health = item.get('health', {})
        if isinstance(saved_health, dict):
            item['health'] = {
                str(model): {
                    'ok': bool(value.get('ok', False)),
                    'error': str(value.get('error') or '')[:160],
                    'checked_at': int(value.get('checked_at', 0) or 0),
                    'latency_ms': int(value.get('latency_ms', 0) or 0),
                    'reply': str(value.get('reply') or '')[:200],
                }
                for model, value in saved_health.items()
                if isinstance(value, dict) and str(model) in models
            }
        else:
            item['health'] = {}
        if item['base_url'] and item['model']:
            normalized.append(item)
    if not normalized:
        normalized = copy.deepcopy(DEFAULT_CONFIG['providers'])
    result['providers'] = normalized
    enabled = [item for item in normalized if item['enabled']]
    if result.get('active_provider') not in {item['id'] for item in enabled}:
        result['active_provider'] = (enabled or normalized)[0]['id']
    result['enabled'] = bool(result.get('enabled', True))
    result['auto_switch'] = bool(result.get('auto_switch', True))
    result['auto_fetch_models'] = bool(result.get('auto_fetch_models', True))
    result['request_timeout'] = min(600, max(5, int(result.get('request_timeout', 120))))
    result['temperature'] = min(2.0, max(0.0, float(result.get('temperature', 0.7))))
    result['max_tokens'] = min(131072, max(1, int(result.get('max_tokens', 8192))))
    result['max_tool_rounds'] = min(20, max(1, int(result.get('max_tool_rounds', 6))))
    result['agent_enabled'] = bool(result.get('agent_enabled', True))
    result['runtime_prompt'] = str(result.get('runtime_prompt') or '')[:30000]
    context = result.get('context') if isinstance(result.get('context'), dict) else {}
    result['context'] = {
        'max_tokens': min(2_000_000, max(0, int(context.get('max_tokens', 65536)))),
        'max_turns': min(1000, max(0, int(context.get('max_turns', 30)))),
        'compress_enabled': bool(context.get('compress_enabled', True)),
        'keep_recent_ratio': min(0.8, max(0.1, float(context.get('keep_recent_ratio', 0.25)))),
    }
    skills = result.get('skills') if isinstance(result.get('skills'), dict) else {}
    enabled_ids = skills.get('enabled_ids', [])
    result['skills'] = {
        'enabled': bool(skills.get('enabled', True)),
        'enabled_ids': list(dict.fromkeys(
            str(item).strip() for item in enabled_ids if str(item).strip()
        ))[:200] if isinstance(enabled_ids, list) else [],
    }
    mcp = result.get('mcp') if isinstance(result.get('mcp'), dict) else {}
    servers = []
    for raw in mcp.get('servers', []) if isinstance(mcp.get('servers'), list) else []:
        if not isinstance(raw, dict):
            continue
        server = {
            'id': str(raw.get('id') or uuid.uuid4().hex[:8]).strip()[:64],
            'name': str(raw.get('name') or raw.get('id') or 'MCP').strip()[:100],
            'endpoint': str(raw.get('endpoint') or '').strip()[:1000],
            'headers': {
                str(key)[:64]: str(val)[:2000]
                for key, val in (raw.get('headers') or {}).items()
            } if isinstance(raw.get('headers'), dict) else {},
            'timeout': min(60, max(5, int(raw.get('timeout', 20)))),
            'enabled': bool(raw.get('enabled', True)),
        }
        if server['id']:
            servers.append(server)
    result['mcp'] = {'enabled': bool(mcp.get('enabled', False)), 'servers': servers[:50]}
    sandbox = result.get('sandbox') if isinstance(result.get('sandbox'), dict) else {}
    result['sandbox'] = {
        'enabled': bool(sandbox.get('enabled', False)),
        'endpoint': str(sandbox.get('endpoint') or '').strip()[:1000],
        'token': str(sandbox.get('token') or '').strip()[:4000],
        'timeout': min(120, max(5, int(sandbox.get('timeout', 30)))),
        'execution_timeout': min(60, max(1, int(sandbox.get('execution_timeout', 20)))),
    }
    subagents = []
    for raw in result.get('subagents', []) if isinstance(result.get('subagents'), list) else []:
        if not isinstance(raw, dict):
            continue
        agent_id = str(raw.get('id') or uuid.uuid4().hex[:8]).strip()[:64]
        if agent_id:
            subagents.append({
                'id': agent_id,
                'name': str(raw.get('name') or agent_id).strip()[:100],
                'description': str(raw.get('description') or '').strip()[:500],
                'system_prompt': str(raw.get('system_prompt') or '')[:30000],
                'provider_id': str(raw.get('provider_id') or '').strip()[:128],
                'model': str(raw.get('model') or '').strip()[:256],
                'enabled': bool(raw.get('enabled', True)),
            })
    result['subagents'] = subagents[:50]
    cron_jobs = []
    for raw in result.get('cron_jobs', []) if isinstance(result.get('cron_jobs'), list) else []:
        if not isinstance(raw, dict):
            continue
        job_id = str(raw.get('id') or uuid.uuid4().hex[:8]).strip()[:64]
        if job_id:
            cron_jobs.append({
                'id': job_id,
                'name': str(raw.get('name') or job_id).strip()[:100],
                'cron': str(raw.get('cron') or '').strip()[:100],
                'interval_seconds': min(31_536_000, max(0, int(raw.get('interval_seconds') or 0))),
                'prompt': str(raw.get('prompt') or '')[:30000],
                'system_prompt': str(raw.get('system_prompt') or '')[:30000],
                'provider_id': str(raw.get('provider_id') or '').strip()[:128],
                'model': str(raw.get('model') or '').strip()[:256],
                'enabled': bool(raw.get('enabled', True)),
            })
    result['cron_jobs'] = cron_jobs[:100]
    return result


class AIService:
    def __init__(self, config: dict, save_callback, data_dir: str = ''):
        self._config = normalize_config(config)
        self._save_callback = save_callback
        self._lock = asyncio.Lock()
        self._health: dict[tuple[str, str], dict] = {}
        for provider in self._config.get('providers', []):
            for model, health in (provider.get('health') or {}).items():
                if isinstance(health, dict):
                    self._health[(provider['id'], model)] = copy.deepcopy(health)
        self.runtime = AgentRuntime(self, data_dir or '.')

    def config(self, *, public: bool = False) -> dict:
        result = copy.deepcopy(self._config)
        if public:
            for provider in result['providers']:
                provider['api_key_set'] = bool(provider.get('api_key'))
                provider['api_key'] = '********' if provider['api_key_set'] else ''
                saved_health = provider.get('health', {})
                for model in provider['models']:
                    health = saved_health.get(model) or self._health.get((provider['id'], model), {})
                    if health:
                        provider.setdefault('health', {})[model] = health
            sandbox = result.get('sandbox', {})
            sandbox['token_set'] = bool(sandbox.get('token'))
            sandbox['token'] = '********' if sandbox['token_set'] else ''
            for server in result.get('mcp', {}).get('servers', []):
                headers = server.get('headers', {})
                server['headers_set'] = bool(headers)
                server['headers'] = {key: '********' for key in headers}
            result['runtime_status'] = self.runtime.status()
        return result

    async def save(self, incoming: dict) -> dict:
        async with self._lock:
            value = copy.deepcopy(incoming) if isinstance(incoming, dict) else {}
            old = {item['id']: item for item in self._config['providers']}
            for provider in value.get('providers', []):
                previous = old.get(str(provider.get('id') or ''))
                if previous and provider.get('api_key') in ('', '********') and provider.get('api_key_set'):
                    provider['api_key'] = previous.get('api_key', '')
                provider.pop('api_key_set', None)
            old_sandbox = self._config.get('sandbox', {})
            sandbox = value.get('sandbox')
            if isinstance(sandbox, dict) and sandbox.get('token') in ('', '********') and sandbox.get('token_set'):
                sandbox['token'] = old_sandbox.get('token', '')
            if isinstance(sandbox, dict):
                sandbox.pop('token_set', None)
            old_servers = {
                item.get('id'): item for item in self._config.get('mcp', {}).get('servers', [])
            }
            for server in (value.get('mcp') or {}).get('servers', []) if isinstance(value.get('mcp'), dict) else []:
                previous = old_servers.get(server.get('id'), {})
                if server.get('headers_set') and all(val == '********' for val in (server.get('headers') or {}).values()):
                    server['headers'] = previous.get('headers', {})
                server.pop('headers_set', None)
            merged = copy.deepcopy(self._config)
            merged.update(value)
            self._config = normalize_config(merged)
            self._save_callback(self._config)
            return self.config(public=True)

    def _provider(self, provider_id: str = '') -> dict | None:
        target = provider_id or self._config['active_provider']
        return next((item for item in self._config['providers'] if item['id'] == target and item['enabled']), None)

    def _candidates(self, provider_id: str = '', model: str = '') -> list[tuple[dict, str]]:
        primary = self._provider(provider_id)
        if primary is None:
            return []
        providers = [primary]
        if self._config['auto_switch'] and not provider_id:
            providers.extend(sorted(
                [item for item in self._config['providers'] if item['enabled'] and item['id'] != primary['id']],
                key=lambda item: (-item['priority'], item['id']),
            ))
        result = []
        for provider in providers:
            if model and provider is primary:
                models = [model]
            elif provider['model_priority_enabled'] and self._config['auto_switch']:
                models = [
                    item for item in provider['model_priority']
                    if item not in provider.get('disabled_models', [])
                ] or [provider['model']]
            else:
                models = [provider['model']]
            result.extend((provider, candidate) for candidate in models if candidate)
        return result

    async def fetch_models(self, provider_id: str) -> list[str]:
        provider = self._provider(provider_id)
        if provider is None:
            raise AIServiceError('接口不存在或未启用')
        headers = {'Accept': 'application/json'}
        if provider.get('api_key'):
            headers['Authorization'] = f"Bearer {provider['api_key']}"
        timeout = aiohttp.ClientTimeout(total=min(60, self._config['request_timeout']))
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(provider['base_url'] + '/models', headers=headers) as response,
        ):
            raw = await response.text()
            if response.status < 200 or response.status >= 300:
                raise AIServiceError(f'HTTP {response.status}: {raw[:200]}')
        try:
            payload = json.loads(raw)
            models = sorted({str(item.get('id')).strip() for item in payload.get('data', []) if isinstance(item, dict) and item.get('id')})
        except (AttributeError, json.JSONDecodeError) as error:
            raise AIServiceError('接口未返回 OpenAI 格式模型列表') from error
        if not models:
            raise AIServiceError('接口未返回模型')
        async with self._lock:
            target = self._provider(provider_id)
            if target:
                target['models'] = models
                target['model_priority'] = [m for m in target['model_priority'] if m in models]
                target['model_priority'].extend(m for m in models if m not in target['model_priority'])
                target['disabled_models'] = [m for m in target.get('disabled_models', []) if m in models]
                target['health'] = {
                    model: health for model, health in (target.get('health') or {}).items()
                    if model in models
                }
                if target['model'] not in models:
                    target['model'] = models[0]
                self._save_callback(self._config)
        return models

    async def probe_models(self, provider_id: str, models: list[str] | None = None) -> list[dict]:
        provider = self._provider(provider_id)
        if provider is None:
            raise AIServiceError('接口不存在或未启用')
        candidates = list(dict.fromkeys(
            str(model).strip() for model in (models or provider.get('models', []))
            if str(model).strip()
        ))[:100]
        if not candidates:
            raise AIServiceError('该接口没有可测活模型')
        semaphore = asyncio.Semaphore(4)

        async def probe(model: str) -> dict:
            started = time.perf_counter()
            try:
                async with semaphore:
                    result = await self._complete_candidate(
                        provider, model, [{'role': 'user', 'content': '你好'}], '',
                        0, 32, None, None, 0,
                    )
                return {
                    'model': model, 'ok': True,
                    'latency_ms': round((time.perf_counter() - started) * 1000),
                    'reply': result['text'][:100], 'error': '',
                }
            except (AIServiceError, aiohttp.ClientError, TimeoutError) as error:
                health = {
                    'ok': False, 'error': str(error)[:160], 'checked_at': int(time.time()),
                }
                self._health[(provider_id, model)] = health
                return {
                    'model': model, 'ok': False,
                    'latency_ms': round((time.perf_counter() - started) * 1000),
                    'reply': '', 'error': health['error'],
                }

        results = await asyncio.gather(*(probe(model) for model in candidates))
        async with self._lock:
            target = next((item for item in self._config['providers'] if item['id'] == provider_id), None)
            if target:
                successful = [item['model'] for item in results if item['ok']]
                failed = [item['model'] for item in results if not item['ok']]
                tested = set(successful + failed)
                old_priority = list(target.get('model_priority') or target.get('models') or [])
                target['model_priority'] = successful + [
                    model for model in old_priority if model not in successful and model in target.get('models', [])
                ]
                target['disabled_models'] = [
                    model for model in target.get('disabled_models', []) if model not in tested
                ] + failed
                health = target.setdefault('health', {})
                for result in results:
                    record = {
                        'ok': bool(result['ok']),
                        'error': str(result.get('error') or '')[:160],
                        'checked_at': int(time.time()),
                        'latency_ms': int(result.get('latency_ms') or 0),
                        'reply': str(result.get('reply') or '')[:200],
                    }
                    health[result['model']] = record
                    self._health[(provider_id, result['model'])] = copy.deepcopy(record)
                self._save_callback(self._config)
        return results

    async def _request(self, provider: dict, payload: dict) -> dict:
        headers = {'Content-Type': 'application/json'}
        if provider.get('api_key'):
            headers['Authorization'] = f"Bearer {provider['api_key']}"
        timeout = aiohttp.ClientTimeout(total=self._config['request_timeout'])
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(
                provider['base_url'] + '/chat/completions', headers=headers, json=payload
            ) as response,
        ):
            raw = await response.text()
            if response.status < 200 or response.status >= 300:
                raise AIServiceError(f'HTTP {response.status}: {raw[:300]}')
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise AIServiceError('接口返回了无效 JSON') from error

    async def _stream_candidate(
        self, provider: dict, model: str, messages: list[dict],
        system_prompt: str, temperature: float | None, max_tokens: int | None,
    ) -> AsyncIterator[dict]:
        payload_messages = copy.deepcopy(messages)
        if system_prompt:
            payload_messages.insert(0, {'role': 'system', 'content': system_prompt})
        payload = {
            'model': model,
            'messages': payload_messages,
            'temperature': self._config['temperature'] if temperature is None else temperature,
            'max_tokens': self._config['max_tokens'] if max_tokens is None else max_tokens,
            'stream': True,
        }
        headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
        if provider.get('api_key'):
            headers['Authorization'] = f"Bearer {provider['api_key']}"
        timeout = aiohttp.ClientTimeout(total=self._config['request_timeout'])

        for attempt in range(2):
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    provider['base_url'] + '/chat/completions',
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status < 200 or response.status >= 300:
                        raw = await response.text()
                        if (
                            attempt == 0
                            and 'max_tokens' in raw.lower()
                            and 'max_completion_tokens' not in payload
                        ):
                            payload['max_completion_tokens'] = payload.pop('max_tokens')
                            continue
                        raise AIServiceError(f'HTTP {response.status}: {raw[:300]}')

                    yield {'type': 'meta', 'provider_id': provider['id'],
                           'provider_name': provider['name'], 'model': model}
                    content_type = response.headers.get('Content-Type', '').lower()
                    if 'event-stream' not in content_type and 'ndjson' not in content_type:
                        raw = await response.text()
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError as error:
                            raise AIServiceError('stream response was not valid JSON') from error
                        content = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
                        if isinstance(content, list):
                            content = ''.join(
                                str(part.get('text') or '') for part in content
                                if isinstance(part, dict)
                            )
                        if content:
                            yield {'type': 'delta', 'text': str(content)}
                        yield {'type': 'done', 'usage': data.get('usage') or {}}
                        return

                    usage = {}
                    saw_delta = False
                    async for raw_line in response.content:
                        line = raw_line.decode('utf-8', errors='replace').strip()
                        if not line or line.startswith(':'):
                            continue
                        if line.startswith('data:'):
                            line = line[5:].strip()
                        if line == '[DONE]':
                            break
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get('error'):
                            raise AIServiceError(str(event['error']))
                        usage = event.get('usage') or usage
                        choices = event.get('choices') or []
                        if not choices:
                            continue
                        delta = (choices[0] or {}).get('delta') or {}
                        content = delta.get('content', '')
                        if isinstance(content, list):
                            content = ''.join(
                                str(part.get('text') or '') for part in content
                                if isinstance(part, dict)
                            )
                        if content:
                            saw_delta = True
                            yield {'type': 'delta', 'text': str(content)}
                    if not saw_delta:
                        raise AIServiceError('stream response contained no text')
                    self._health[(provider['id'], model)] = {
                        'ok': True, 'error': '', 'checked_at': int(time.time()),
                    }
                    yield {'type': 'done', 'usage': usage}
                    return

        raise AIServiceError('stream request failed')

    async def stream_complete(
        self,
        messages: list[dict],
        *,
        system_prompt: str = '',
        provider_id: str = '',
        model: str = '',
        temperature: float | None = None,
        max_tokens: int | None = None,
        session_id: str = '',
        runtime_prompt: str = '',
    ) -> AsyncIterator[dict]:
        if not self._config['enabled']:
            raise AIServiceError('AI module is disabled')
        run_id = self.runtime.begin_run(session_id)
        error_text = ''
        started_output = False
        try:
            candidates = self._candidates(provider_id, model)
            if not candidates:
                raise AIServiceError('no available AI provider')
            prepared_messages = (
                self.runtime.prepare_context(messages)
                if self._config.get('context', {}).get('compress_enabled', True)
                else copy.deepcopy(messages)
            )
            prompts = [self._config.get('runtime_prompt', ''), runtime_prompt, system_prompt]
            combined_prompt = '\n\n'.join(item.strip() for item in prompts if str(item).strip())
            last_error = None
            for provider, candidate_model in candidates:
                try:
                    async for event in self._stream_candidate(
                        provider, candidate_model, prepared_messages, combined_prompt,
                        temperature, max_tokens,
                    ):
                        event['run_id'] = run_id
                        if event.get('type') == 'delta':
                            started_output = True
                        yield event
                    return
                except (AIServiceError, aiohttp.ClientError, TimeoutError) as error:
                    last_error = error
                    self._health[(provider['id'], candidate_model)] = {
                        'ok': False, 'error': str(error)[:160], 'checked_at': int(time.time()),
                    }
                    if started_output or not self._config['auto_switch'] or model:
                        raise
            raise last_error or AIServiceError('all AI providers failed')
        except asyncio.CancelledError:
            error_text = 'interrupted'
            raise
        except Exception as error:
            error_text = str(error)
            raise
        finally:
            self.runtime.finish_run(run_id, error_text)

    async def complete(
        self,
        messages: list[dict],
        *,
        system_prompt: str = '',
        provider_id: str = '',
        model: str = '',
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_handler: ToolHandler | None = None,
        max_tool_rounds: int | None = None,
        session_id: str = '',
        runtime_prompt: str = '',
        enable_runtime_tools: bool = True,
        allow_handoff: bool = True,
    ) -> dict:
        if not self._config['enabled']:
            raise AIServiceError('AI LLM 服务未启用')
        run_id = self.runtime.begin_run(session_id)
        error_text = ''
        try:
            candidates = self._candidates(provider_id, model)
            if not candidates:
                raise AIServiceError('没有可用的 AI 接口')
            prepared_messages = (
                self.runtime.prepare_context(messages)
                if self._config.get('context', {}).get('compress_enabled', True)
                else copy.deepcopy(messages)
            )
            prompts = [self._config.get('runtime_prompt', ''), system_prompt, runtime_prompt]
            combined_prompt = '\n\n'.join(item.strip() for item in prompts if str(item).strip())
            runtime_tools = await self.runtime.tools(allow_handoff=allow_handoff) if enable_runtime_tools else []
            all_tools = list(tools or [])
            known = {item.get('function', {}).get('name') for item in all_tools}
            all_tools.extend(item for item in runtime_tools if item['function']['name'] not in known)

            async def combined_handler(name: str, arguments: dict):
                internal = await self.runtime.call_tool(name, arguments)
                if internal is not None:
                    return internal
                if tool_handler is None:
                    return {'ok': False, 'error': '工具不可用'}
                value = tool_handler(name, arguments)
                return await value if asyncio.iscoroutine(value) else value

            last_error = None
            for provider, candidate_model in candidates:
                try:
                    result = await self._complete_candidate(
                        provider, candidate_model, prepared_messages, combined_prompt,
                        temperature, max_tokens, all_tools or None,
                        combined_handler if all_tools else None, max_tool_rounds,
                    )
                    result['run_id'] = run_id
                    return result
                except (AIServiceError, aiohttp.ClientError, TimeoutError) as error:
                    last_error = error
                    self._health[(provider['id'], candidate_model)] = {
                        'ok': False, 'error': str(error)[:160], 'checked_at': int(time.time()),
                    }
                    if not self._config['auto_switch'] or model:
                        raise
            raise last_error or AIServiceError('所有接口均不可用')
        except asyncio.CancelledError:
            error_text = 'interrupted'
            raise
        except Exception as error:
            error_text = str(error)
            raise
        finally:
            self.runtime.finish_run(run_id, error_text)

    async def _complete_candidate(
        self, provider, model, messages, system_prompt, temperature,
        max_tokens, tools, tool_handler, max_tool_rounds,
    ) -> dict:
        payload_messages = copy.deepcopy(messages)
        if system_prompt:
            payload_messages.insert(0, {'role': 'system', 'content': system_prompt})
        payload = {
            'model': model,
            'messages': payload_messages,
            'temperature': self._config['temperature'] if temperature is None else temperature,
            'max_tokens': self._config['max_tokens'] if max_tokens is None else max_tokens,
        }
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'auto'
        rounds = self._config['max_tool_rounds'] if max_tool_rounds is None else max_tool_rounds
        for _round in range(max(1, rounds + 1)):
            try:
                data = await self._request(provider, payload)
            except AIServiceError as error:
                if 'max_tokens' in str(error).lower() and 'max_completion_tokens' not in payload:
                    payload['max_completion_tokens'] = payload.pop('max_tokens')
                    data = await self._request(provider, payload)
                else:
                    raise
            try:
                message = data['choices'][0]['message']
            except (KeyError, IndexError, TypeError) as error:
                raise AIServiceError('接口返回中没有 choices[0].message') from error
            tool_calls = message.get('tool_calls') or []
            if not tool_calls:
                content = message.get('content')
                if isinstance(content, list):
                    content = ''.join(str(part.get('text') or '') for part in content if isinstance(part, dict))
                text = str(content or '').strip()
                if not text:
                    raise AIServiceError('接口返回了空消息')
                self._health[(provider['id'], model)] = {
                    'ok': True, 'error': '', 'checked_at': int(time.time()),
                }
                return {
                    'text': text,
                    'provider_id': provider['id'],
                    'provider_name': provider['name'],
                    'model': model,
                    'usage': data.get('usage') or {},
                }
            if tool_handler is None or _round >= rounds:
                raise AIServiceError('模型请求了不可用工具或达到工具轮数上限')
            payload['messages'].append({
                'role': 'assistant',
                'content': message.get('content') or '',
                'tool_calls': tool_calls,
            })
            for call in tool_calls[:8]:
                function = call.get('function') or {}
                try:
                    arguments = json.loads(function.get('arguments') or '{}')
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
                result = tool_handler(str(function.get('name') or ''), arguments)
                if asyncio.iscoroutine(result):
                    result = await result
                payload['messages'].append({
                    'role': 'tool',
                    'tool_call_id': str(call.get('id') or ''),
                    'name': str(function.get('name') or ''),
                    'content': json.dumps(result, ensure_ascii=False, default=str)[:12000],
                })
        raise AIServiceError('达到工具轮数上限')
