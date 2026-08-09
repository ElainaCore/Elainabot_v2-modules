"""Shared OpenAI-compatible LLM service for ElainaBot plugins."""
from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from xml.etree import ElementTree

import aiohttp

from .audit import InvocationAudit
from .runtime import AgentRuntime

ToolHandler = Callable[[str, dict], Awaitable[dict] | dict]
CompletionValidator = Callable[[str, list[dict]], str | None]


class AIServiceError(RuntimeError):
    pass


class AIProviderError(AIServiceError):
    """A provider transport or protocol failure eligible for failover."""

    pass


class AIExecutionIncomplete(AIServiceError):
    """The provider answered, but an agent task did not finish its required actions."""

    execution_incomplete = True


def _xml_scalar(value: str):
    text = str(value or '').strip()
    if not text:
        return ''
    if text.lower() == 'true':
        return True
    if text.lower() == 'false':
        return False
    if text.lower() == 'null':
        return None
    if text[:1] in ('{', '[', '"') or re.fullmatch(r'-?(?:\d+\.?\d*|\.\d+)', text):
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
    return text


def _xml_element_value(element: ElementTree.Element):
    children = list(element)
    if not children:
        return _xml_scalar(element.text or '')
    result = {}
    for child in children:
        value = _xml_element_value(child)
        if child.tag in result:
            if not isinstance(result[child.tag], list):
                result[child.tag] = [result[child.tag]]
            result[child.tag].append(value)
        else:
            result[child.tag] = value
    return result


def _xml_tool_calls(content: str, tools: list[dict] | None) -> tuple[list[dict], str]:
    """Convert XML-style calls emitted by some compatible endpoints into tool calls.

    Only names present in the supplied tool schema are accepted. Complete XML blocks
    are parsed with ElementTree; a bare opening tag is accepted only for a tool with
    no required arguments.
    """
    if not content or not tools or '<' not in content:
        return [], content
    definitions = {}
    for item in tools:
        function = item.get('function', {}) if isinstance(item, dict) else {}
        name = str(function.get('name') or '')
        if name:
            parameters = function.get('parameters') or {}
            definitions[name] = set(parameters.get('required') or [])
    if not definitions:
        return [], content

    matches: list[tuple[int, int, str, dict]] = []
    occupied: list[tuple[int, int]] = []
    for name in definitions:
        escaped = re.escape(name)
        block_pattern = re.compile(
            rf'<{escaped}\s*>(.*?)</{escaped}\s*>', re.IGNORECASE | re.DOTALL,
        )
        for match in block_pattern.finditer(content):
            try:
                root = ElementTree.fromstring(match.group(0))
                value = _xml_element_value(root)
                arguments = value if isinstance(value, dict) else {}
            except ElementTree.ParseError:
                continue
            matches.append((match.start(), match.end(), name, arguments))
            occupied.append((match.start(), match.end()))
        self_pattern = re.compile(rf'<{escaped}\s*/>', re.IGNORECASE)
        for match in self_pattern.finditer(content):
            matches.append((match.start(), match.end(), name, {}))
            occupied.append((match.start(), match.end()))

    def is_occupied(position: int) -> bool:
        return any(start <= position < end for start, end in occupied)

    for name, required in definitions.items():
        if required:
            continue
        opening_pattern = re.compile(rf'<{re.escape(name)}\s*>', re.IGNORECASE)
        for match in opening_pattern.finditer(content):
            if not is_occupied(match.start()):
                matches.append((match.start(), match.end(), name, {}))

    matches.sort(key=lambda item: item[0])
    calls = []
    cleaned = content
    for start, end, name, arguments in matches[:8]:
        calls.append({
            'id': f'xml_{uuid.uuid4().hex}',
            'type': 'function',
            'function': {
                'name': name,
                'arguments': json.dumps(arguments, ensure_ascii=False),
            },
        })
    for start, end, _name, _arguments in sorted(matches[:8], reverse=True):
        cleaned = cleaned[:start] + cleaned[end:]
    return calls, cleaned.strip()


def _text_tool_protocol(tools: list[dict] | None) -> str:
    """Describe a constrained XML fallback for models without function calling."""
    definitions = []
    for item in tools or []:
        function = item.get('function', {}) if isinstance(item, dict) else {}
        name = str(function.get('name') or '').strip()
        if not name:
            continue
        parameters = function.get('parameters') or {}
        properties = parameters.get('properties') or {}
        required = set(parameters.get('required') or [])
        arguments = ', '.join(
            f"{key}{'*' if key in required else ''}" for key in properties
        ) or '无参数'
        definitions.append(f'{name}({arguments})')
    if not definitions:
        return ''
    return (
        '工具文本兼容协议：优先使用接口原生 tool_calls。若当前模型不能输出原生工具调用，'
        '则在需要调用工具时只输出 XML，不要同时输出解释文字。格式为 '
        '<工具名><参数名>参数值</参数名></工具名>；无参数工具使用 <工具名/>。'
        '对象或数组参数写成 JSON 文本，XML 特殊字符必须转义。一次最多调用 8 个工具。'
        '只能使用以下工具，星号表示必填参数：' + '；'.join(definitions)
    )


def _tools_parameter_unsupported(error_text: str) -> bool:
    text = str(error_text or '').casefold()
    tool_marker = any(marker in text for marker in (
        'tools', 'function_call', 'function calling', '函数调用',
    ))
    unsupported = any(marker in text for marker in (
        'unsupported', 'not support', 'unknown', 'unrecognized', 'invalid',
        'extra_forbidden', '不支持', '未知参数', '无效参数',
    ))
    return tool_marker and unsupported


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
    'audit_include_content': False,
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
    'plugin_capabilities': [],
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
    result['audit_include_content'] = bool(result.get('audit_include_content', False))
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
    capabilities = []
    seen_capabilities = set()
    for raw in result.get('plugin_capabilities', []) if isinstance(result.get('plugin_capabilities'), list) else []:
        if not isinstance(raw, dict):
            continue
        source = str(raw.get('source_plugin') or '').strip()[:128]
        kind = str(raw.get('kind') or '').strip().lower()
        capability_id = str(raw.get('id') or '').strip()[:128]
        if not source or kind not in {'skill', 'agent', 'mcp', 'tool'} or not capability_id:
            continue
        key = f'{source}:{kind}:{capability_id}'
        if key in seen_capabilities:
            continue
        seen_capabilities.add(key)
        capabilities.append({
            'key': key,
            'id': capability_id,
            'kind': kind,
            'source_plugin': source,
            'name': str(raw.get('name') or capability_id).strip()[:100],
            'description': str(raw.get('description') or '').strip()[:500],
            'enabled': bool(raw.get('enabled', True)),
            'shared': bool(raw.get('shared', kind == 'tool')),
            'allowed_consumers': list(dict.fromkeys(
                str(item).strip()[:128]
                for item in raw.get('allowed_consumers', [])
                if str(item).strip()
            ))[:100] if isinstance(raw.get('allowed_consumers'), list) else [],
            'shared_configured': bool(raw.get('shared_configured', False)),
            'content': str(raw.get('content') or '')[:30000],
            'config': copy.deepcopy(raw.get('config')) if isinstance(raw.get('config'), dict) else {},
        })
    result['plugin_capabilities'] = capabilities[:500]
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
        self._capability_handlers: dict[str, ToolHandler] = {}
        self._online_capabilities: set[str] = set()
        for provider in self._config.get('providers', []):
            for model, health in (provider.get('health') or {}).items():
                if isinstance(health, dict):
                    self._health[(provider['id'], model)] = copy.deepcopy(health)
        self.runtime = AgentRuntime(self, data_dir or '.')
        self.audit = InvocationAudit(data_dir or '.')
        self.audit.set_include_content(self._config.get('audit_include_content', False))

    @staticmethod
    def _capability_allowed(item: dict, consumer_plugin: str = '') -> bool:
        consumer = str(consumer_plugin or '').strip()
        if not item.get('enabled'):
            return False
        if not consumer:
            return False
        return (
            consumer == item.get('source_plugin')
            or consumer in set(item.get('allowed_consumers') or [])
            or bool(item.get('shared'))
        )

    def plugin_capabilities(
        self, *, consumer_plugin: str = '', kind: str = '', public: bool = False,
    ) -> list[dict]:
        result = []
        for item in self._config.get('plugin_capabilities', []):
            if kind and item.get('kind') != kind:
                continue
            if consumer_plugin and not self._capability_allowed(item, consumer_plugin):
                continue
            value = copy.deepcopy(item)
            value['online'] = value['key'] in self._online_capabilities
            if public:
                value.pop('config', None)
            result.append(value)
        return result

    def register_plugin_capability(
        self, source_plugin: str, kind: str, definition: dict, handler: ToolHandler | None = None,
    ) -> dict:
        source = str(source_plugin or '').strip()[:128]
        capability_kind = str(kind or '').strip().lower()
        capability_id = str((definition or {}).get('id') or '').strip()[:128]
        if not source or capability_kind not in {'skill', 'agent', 'mcp', 'tool'} or not capability_id:
            raise ValueError('插件能力必须提供合法的 source_plugin、kind 和 id')
        key = f'{source}:{capability_kind}:{capability_id}'
        current = next((
            item for item in self._config.get('plugin_capabilities', []) if item.get('key') == key
        ), None)
        incoming = copy.deepcopy(definition or {})
        incoming.update({'key': key, 'id': capability_id, 'kind': capability_kind, 'source_plugin': source})
        if current is not None:
            for field in ('enabled', 'content', 'allowed_consumers'):
                if field in current:
                    incoming[field] = copy.deepcopy(current[field])
            if current.get('shared_configured'):
                incoming['shared'] = bool(current.get('shared'))
                incoming['shared_configured'] = True
            else:
                incoming['shared'] = capability_kind == 'tool'
                incoming['shared_configured'] = False
            records = [
                incoming if item.get('key') == key else item
                for item in self._config.get('plugin_capabilities', [])
            ]
        else:
            incoming.setdefault('enabled', True)
            incoming.setdefault('shared', capability_kind == 'tool')
            incoming.setdefault('shared_configured', False)
            records = [*self._config.get('plugin_capabilities', []), incoming]
        merged = copy.deepcopy(self._config)
        merged['plugin_capabilities'] = records
        self._config = normalize_config(merged)
        if handler is not None:
            self._capability_handlers[key] = handler
        self._online_capabilities.add(key)
        self._save_callback(self._config)
        return next(item for item in self.plugin_capabilities() if item['key'] == key)

    def unregister_plugin_capabilities(self, source_plugin: str) -> None:
        prefix = str(source_plugin or '').strip() + ':'
        for key in [key for key in self._capability_handlers if key.startswith(prefix)]:
            self._capability_handlers.pop(key, None)
        self._online_capabilities = {
            key for key in self._online_capabilities if not key.startswith(prefix)
        }

    async def save_plugin_capabilities(self, incoming: list[dict]) -> list[dict]:
        async with self._lock:
            updates = {
                str(item.get('key') or ''): item for item in incoming if isinstance(item, dict)
            }
            records = []
            for current in self._config.get('plugin_capabilities', []):
                update = updates.get(current.get('key'), {})
                value = copy.deepcopy(current)
                for field in ('enabled', 'shared', 'allowed_consumers', 'content'):
                    if field in update:
                        value[field] = copy.deepcopy(update[field])
                if 'shared' in update:
                    value['shared_configured'] = True
                records.append(value)
            merged = copy.deepcopy(self._config)
            merged['plugin_capabilities'] = records
            self._config = normalize_config(merged)
            self.audit.set_include_content(self._config.get('audit_include_content', False))
            self._save_callback(self._config)
            return self.plugin_capabilities(public=True)

    def capability_handler(self, key: str):
        return self._capability_handlers.get(str(key or ''))

    def list_capabilities(self, consumer_plugin: str, kind: str = '') -> list[dict]:
        """List online capabilities that a plugin may discover and call."""
        result = []
        for item in self.plugin_capabilities(
            consumer_plugin=consumer_plugin, kind=str(kind or '').lower(),
        ):
            if not item.get('online'):
                continue
            value = {
                key: copy.deepcopy(item.get(key))
                for key in (
                    'key', 'id', 'kind', 'source_plugin', 'name', 'description',
                    'enabled', 'shared', 'online',
                )
            }
            if item.get('kind') == 'tool':
                value['input_schema'] = copy.deepcopy(
                    (item.get('config') or {}).get('schema') or {
                        'type': 'object', 'properties': {},
                    }
                )
            result.append(value)
        return result

    async def discover_capabilities(
        self, consumer_plugin: str, kind: str = '',
    ) -> list[dict]:
        """Discover callable capabilities, including tools exposed by MCP entries."""
        result = self.list_capabilities(consumer_plugin, kind)
        if not kind or str(kind).lower() == 'mcp':
            discovered = await self.runtime.refresh_plugin_mcp_tools(consumer_plugin)
            by_key: dict[str, list[dict]] = {}
            for tool in discovered:
                by_key.setdefault(str(tool.get('capability_key') or ''), []).append({
                    'name': tool.get('original_name'),
                    'call_name': tool.get('name'),
                    'input_schema': copy.deepcopy(
                        self.runtime._mcp_schemas.get(str(tool.get('name') or ''), {}).get(
                            'inputSchema', {'type': 'object', 'properties': {}}
                        )
                    ),
                })
            for item in result:
                if item.get('kind') == 'mcp':
                    item['tools'] = by_key.get(str(item.get('key') or ''), [])
        return result

    async def call_capability(
        self, consumer_plugin: str, capability_key: str, arguments: dict | None = None,
    ):
        """Call an allowed capability by the key returned from discover_capabilities()."""
        arguments = arguments if isinstance(arguments, dict) else {}
        item = next((value for value in self.plugin_capabilities(
            consumer_plugin=consumer_plugin,
        ) if value.get('key') == str(capability_key or '') and value.get('online')), None)
        if item is None:
            raise AIServiceError('能力不存在、未开启共享或当前插件无权使用')
        if item['kind'] == 'skill':
            return {'ok': True, 'capability_key': item['key'], 'content': item.get('content', '')}
        if item['kind'] == 'agent':
            return await self.runtime._delegate_plugin_agent({
                'capability_key': item['key'], 'task': str(arguments.get('task') or ''),
            }, consumer_plugin)
        if item['kind'] == 'tool':
            handler = self.capability_handler(item['key'])
            if handler is None:
                return {'ok': False, 'error': '插件能力当前不在线'}
            value = handler(item['id'], arguments)
            return await value if asyncio.iscoroutine(value) else value
        if item['kind'] == 'mcp':
            await self.runtime.refresh_plugin_mcp_tools(consumer_plugin)
            tool_name = str(arguments.get('tool') or '')
            tool_arguments = arguments.get('arguments')
            tool_arguments = tool_arguments if isinstance(tool_arguments, dict) else {}
            for call_name, (capability, _server, original) in self.runtime._plugin_mcp_tools.get(
                consumer_plugin, {}
            ).items():
                if capability.get('key') == item['key'] and original == tool_name:
                    return await self.runtime.call_tool(
                        call_name, tool_arguments, consumer_plugin=consumer_plugin,
                    )
            return {'ok': False, 'error': 'MCP 工具不存在或服务当前不可用'}
        return {'ok': False, 'error': '不支持的能力类型'}

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
            result['plugin_capabilities'] = self.plugin_capabilities(public=True)
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
            self.audit.set_include_content(self._config.get('audit_include_content', False))
            self._save_callback(self._config)
            return self.config(public=True)

    def _provider(self, provider_id: str = '') -> dict | None:
        target = provider_id or self._config['active_provider']
        return next((item for item in self._config['providers'] if item['id'] == target and item['enabled']), None)

    def available(self) -> bool:
        """Return whether the service has at least one enabled, usable model."""
        return bool(self._config.get('enabled') and self._candidates())

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
            if model:
                disabled = set(provider.get('disabled_models', []))
                catalog = {
                    *(provider.get('models') or []),
                    *(provider.get('model_priority') or []),
                    provider.get('model'),
                }
                models = [model] if model in catalog and model not in disabled else []
            elif provider['model_priority_enabled'] and self._config['auto_switch']:
                models = [
                    item for item in provider['model_priority']
                    if item not in provider.get('disabled_models', [])
                ]
            else:
                models = (
                    [provider['model']]
                    if provider['model'] not in provider.get('disabled_models', [])
                    else []
                )
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

    async def moderate(
        self, text: str, *, provider_id: str = '', model: str = 'omni-moderation-latest',
    ) -> dict:
        """Run the provider's dedicated OpenAI-compatible Moderation API."""
        provider = self._provider(provider_id)
        if provider is None:
            raise AIServiceError('没有可用的内容审核接口')
        value = str(text or '').strip()
        if not value:
            return {'flagged': False, 'categories': [], 'model': model}
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if provider.get('api_key'):
            headers['Authorization'] = f"Bearer {provider['api_key']}"
        timeout = aiohttp.ClientTimeout(total=min(30, self._config['request_timeout']))
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(
                    provider['base_url'] + '/moderations', headers=headers,
                    json={'model': model, 'input': value},
                ) as response,
            ):
                raw = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise AIServiceError(f'内容审核接口 HTTP {response.status}: {raw[:200]}')
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as error:
            raise AIServiceError(f'内容审核接口不可用：{error}') from error
        try:
            payload = json.loads(raw)
            item = (payload.get('results') or [])[0]
            categories = item.get('categories') or {}
        except (AttributeError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise AIServiceError('内容审核接口返回格式无效') from error
        return {
            'flagged': bool(item.get('flagged')),
            'categories': [str(name) for name, hit in categories.items() if hit],
            'model': str(payload.get('model') or model),
        }

    async def generate_image(
        self, prompt: str, *, candidates: list[dict], size: str = '1024x1024',
        reference_image: bytes | None = None,
    ) -> dict:
        """Generate one image and fail over across an explicit provider/model route."""
        value = str(prompt or '').strip()
        if not value:
            raise AIServiceError('生图描述不能为空')
        route = []
        seen = set()
        for item in candidates or []:
            if not isinstance(item, dict) or not item.get('enabled', True):
                continue
            provider_id = str(item.get('provider_id') or '').strip()
            model = str(item.get('model') or '').strip()
            key = (provider_id, model)
            if not provider_id or not model or key in seen:
                continue
            provider = self._provider(provider_id)
            if provider is not None:
                route.append((provider, model))
                seen.add(key)
        if not route:
            raise AIServiceError('没有配置可用的生图接口与模型')
        image_size = size if size in {'256x256', '512x512', '1024x1024', '1024x1536', '1536x1024'} else '1024x1024'
        errors = []
        timeout = aiohttp.ClientTimeout(total=min(180, max(30, self._config['request_timeout'])))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for provider, model in route:
                headers = {'Accept': 'application/json'}
                if provider.get('api_key'):
                    headers['Authorization'] = f"Bearer {provider['api_key']}"
                try:
                    if reference_image:
                        form = aiohttp.FormData()
                        form.add_field('model', model)
                        form.add_field('prompt', value)
                        form.add_field('size', image_size)
                        form.add_field('n', '1')
                        form.add_field(
                            'image', reference_image, filename='persona.png',
                            content_type='image/png',
                        )
                        request = session.post(
                            provider['base_url'] + '/images/edits', headers=headers, data=form,
                        )
                    else:
                        request = session.post(
                            provider['base_url'] + '/images/generations',
                            headers={**headers, 'Content-Type': 'application/json'},
                            json={'model': model, 'prompt': value, 'size': image_size, 'n': 1},
                        )
                    async with request as response:
                        raw = await response.text()
                        if response.status < 200 or response.status >= 300:
                            raise AIProviderError(f'HTTP {response.status}: {raw[:200]}')
                    payload = json.loads(raw)
                    item = (payload.get('data') or [])[0]
                    url = str(item.get('url') or '').strip()
                    encoded = str(item.get('b64_json') or '').strip()
                    if not url and not encoded:
                        raise AIProviderError('接口未返回 url 或 b64_json')
                    return {
                        'url': url, 'b64_json': encoded, 'provider_id': provider['id'],
                        'provider': provider.get('name') or provider['id'], 'model': model,
                    }
                except (
                    aiohttp.ClientError, asyncio.TimeoutError, TimeoutError,
                    AttributeError, IndexError, TypeError, json.JSONDecodeError, AIProviderError,
                ) as error:
                    errors.append(f"{provider.get('name') or provider['id']}/{model}: {error}")
        raise AIServiceError('所有生图接口均失败：' + '；'.join(errors)[:1000])

    async def probe_models(
        self, provider_id: str, models: list[str] | None = None, *, apply_results: bool = False,
    ) -> list[dict]:
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
                if apply_results:
                    successful = [item['model'] for item in results if item['ok']]
                    failed = [item['model'] for item in results if not item['ok']]
                    tested = set(successful + failed)
                    old_priority = list(target.get('model_priority') or target.get('models') or [])
                    target['model_priority'] = successful + [
                        model for model in old_priority
                        if model not in successful and model in target.get('models', [])
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

    async def _request(self, provider: dict, payload: dict, run_id: str = '') -> dict:
        headers = {'Content-Type': 'application/json'}
        if provider.get('api_key'):
            headers['Authorization'] = f"Bearer {provider['api_key']}"
        timeout = aiohttp.ClientTimeout(total=self._config['request_timeout'])
        attempt_id = self.audit.attempt_start(
            run_id, provider, str(payload.get('model') or ''), payload,
        ) if run_id else ''
        started = time.perf_counter()
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(
                    provider['base_url'] + '/chat/completions', headers=headers, json=payload
                ) as response,
            ):
                first_byte_ms = round((time.perf_counter() - started) * 1000)
                raw = await response.text()
                if response.status < 200 or response.status >= 300:
                    if run_id:
                        self.audit.attempt_finish(
                            run_id, attempt_id, status='error', http_status=response.status,
                            response=raw, error=f'HTTP {response.status}', ttfb_ms=first_byte_ms,
                            response_headers=dict(response.headers),
                        )
                    raise AIProviderError(f'HTTP {response.status}: {raw[:300]}')
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as error:
            if run_id:
                self.audit.attempt_finish(
                    run_id, attempt_id, status='error', http_status=None, error=str(error),
                )
            raise
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            if run_id:
                self.audit.attempt_finish(
                    run_id, attempt_id, status='error', http_status=response.status,
                    response=raw, error='invalid JSON', ttfb_ms=first_byte_ms,
                    response_headers=dict(response.headers),
                )
            raise AIProviderError('接口返回了无效 JSON') from error
        if run_id:
            self.audit.attempt_finish(
                run_id, attempt_id, status='success', http_status=response.status,
                response=data, usage=data.get('usage') or {}, ttfb_ms=first_byte_ms,
                response_headers=dict(response.headers),
            )
        return data

    async def _stream_candidate(
        self, provider: dict, model: str, messages: list[dict],
        system_prompt: str, temperature: float | None, max_tokens: int | None, run_id: str = '',
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
            attempt_id = self.audit.attempt_start(run_id, provider, model, payload) if run_id else ''
            request_started = time.perf_counter()
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    provider['base_url'] + '/chat/completions',
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status < 200 or response.status >= 300:
                        raw = await response.text()
                        elapsed = round((time.perf_counter() - request_started) * 1000)
                        if run_id:
                            self.audit.attempt_finish(
                                run_id, attempt_id, status='error', http_status=response.status,
                                response=raw, error=f'HTTP {response.status}', ttfb_ms=elapsed,
                                response_headers=dict(response.headers),
                            )
                        if (
                            attempt == 0
                            and 'max_tokens' in raw.lower()
                            and 'max_completion_tokens' not in payload
                        ):
                            payload['max_completion_tokens'] = payload.pop('max_tokens')
                            continue
                        raise AIProviderError(f'HTTP {response.status}: {raw[:300]}')

                    yield {'type': 'meta', 'provider_id': provider['id'],
                           'provider_name': provider['name'], 'model': model}
                    content_type = response.headers.get('Content-Type', '').lower()
                    if 'event-stream' not in content_type and 'ndjson' not in content_type:
                        raw = await response.text()
                        elapsed = round((time.perf_counter() - request_started) * 1000)
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError as error:
                            raise AIProviderError('stream response was not valid JSON') from error
                        content = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
                        if isinstance(content, list):
                            content = ''.join(
                                str(part.get('text') or '') for part in content
                                if isinstance(part, dict)
                            )
                        if content:
                            yield {'type': 'delta', 'text': str(content)}
                        if run_id:
                            self.audit.attempt_finish(
                                run_id, attempt_id, status='success', http_status=response.status,
                                response=data, usage=data.get('usage') or {}, ttfb_ms=elapsed,
                                response_headers=dict(response.headers),
                            )
                        yield {'type': 'done', 'usage': data.get('usage') or {}}
                        return

                    usage = {}
                    saw_delta = False
                    response_text = []
                    first_token_ms = None
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
                            raise AIProviderError(str(event['error']))
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
                            if first_token_ms is None:
                                first_token_ms = round((time.perf_counter() - request_started) * 1000)
                            saw_delta = True
                            response_text.append(str(content))
                            yield {'type': 'delta', 'text': str(content)}
                    if not saw_delta:
                        raise AIProviderError('stream response contained no text')
                    self._health[(provider['id'], model)] = {
                        'ok': True, 'error': '', 'checked_at': int(time.time()),
                    }
                    if run_id:
                        self.audit.attempt_finish(
                            run_id, attempt_id, status='success', http_status=response.status,
                            response={'text': ''.join(response_text)}, usage=usage,
                            ttfb_ms=first_token_ms, response_headers=dict(response.headers),
                        )
                    yield {'type': 'done', 'usage': usage}
                    return

        raise AIProviderError('stream request failed')

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
        prepare_context: bool = True,
    ) -> AsyncIterator[dict]:
        if not self._config['enabled']:
            raise AIServiceError('AI module is disabled')
        run_id = self.runtime.begin_run(session_id)
        error_text = ''
        started_output = False
        final_text = []
        final_usage = {}
        self.audit.start(run_id, kind='stream', session_id=session_id, consumer_plugin='', request={
            'messages': messages, 'system_prompt': system_prompt, 'runtime_prompt': runtime_prompt,
            'provider_id': provider_id, 'model': model, 'temperature': temperature,
            'max_tokens': max_tokens,
        })
        try:
            candidates = self._candidates(provider_id, model)
            if not candidates:
                raise AIServiceError('no available AI provider')
            prepared_messages = (
                self.runtime.prepare_context(messages)
                if prepare_context and self._config.get('context', {}).get('compress_enabled', True)
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
                        run_id,
                    ):
                        event['run_id'] = run_id
                        if event.get('type') == 'delta':
                            started_output = True
                            final_text.append(str(event.get('text') or ''))
                        elif event.get('type') == 'done':
                            final_usage = event.get('usage') or {}
                        yield event
                    return
                except (AIProviderError, aiohttp.ClientError, TimeoutError) as error:
                    last_error = error
                    self.audit.fail_running_attempt(run_id, str(error))
                    self._health[(provider['id'], candidate_model)] = {
                        'ok': False, 'error': str(error)[:160], 'checked_at': int(time.time()),
                    }
                    self.audit.event(run_id, 'failover', {
                        'provider_id': provider['id'], 'model': candidate_model, 'error': str(error),
                    })
                    if started_output or not self._config['auto_switch'] or model:
                        raise
            raise last_error or AIServiceError('all AI providers failed')
        except asyncio.CancelledError:
            error_text = 'interrupted'
            self.audit.fail_running_attempt(run_id, error_text)
            raise
        except Exception as error:
            error_text = str(error)
            raise
        finally:
            self.audit.finish(
                run_id, response={'text': ''.join(final_text)}, usage=final_usage, error=error_text,
            )
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
        enable_runtime_tools: bool = False,
        allow_handoff: bool = True,
        consumer_plugin: str = '',
        runtime_capabilities: list[str] | None = None,
        required_tools: list[str] | None = None,
        prepare_context: bool = True,
        completion_validator: CompletionValidator | None = None,
    ) -> dict:
        if not self._config['enabled']:
            raise AIServiceError('AI LLM 服务未启用')
        run_id = self.runtime.begin_run(session_id)
        error_text = ''
        final_result = None
        self.audit.start(run_id, kind='complete', session_id=session_id, consumer_plugin=consumer_plugin, request={
            'messages': messages, 'system_prompt': system_prompt, 'runtime_prompt': runtime_prompt,
            'provider_id': provider_id, 'model': model, 'temperature': temperature,
            'max_tokens': max_tokens, 'tools': tools or [],
            'enable_runtime_tools': enable_runtime_tools, 'runtime_capabilities': runtime_capabilities or [],
            'required_tools': required_tools or [],
            'prepare_context': bool(prepare_context),
        })
        try:
            candidates = self._candidates(provider_id, model)
            if not candidates:
                raise AIServiceError('没有可用的 AI 接口')
            prepared_messages = (
                self.runtime.prepare_context(messages)
                if prepare_context and self._config.get('context', {}).get('compress_enabled', True)
                else copy.deepcopy(messages)
            )
            prompts = [self._config.get('runtime_prompt', ''), system_prompt, runtime_prompt]
            combined_prompt = '\n\n'.join(item.strip() for item in prompts if str(item).strip())
            runtime_tools = await self.runtime.tools(
                allow_handoff=allow_handoff,
                consumer_plugin=consumer_plugin,
                capability_types=runtime_capabilities,
            ) if enable_runtime_tools else []
            caller_tools = list(tools or [])
            caller_tool_names = {
                str(item.get('function', {}).get('name') or '')
                for item in caller_tools if isinstance(item, dict)
            }
            caller_tool_names.discard('')
            all_tools = list(caller_tools)
            known = {item.get('function', {}).get('name') for item in all_tools}
            all_tools.extend(item for item in runtime_tools if item['function']['name'] not in known)

            async def combined_handler(name: str, arguments: dict):
                tool_id = self.audit.tool_start(run_id, name, arguments)
                try:
                    if tool_handler is not None and name in caller_tool_names:
                        value = tool_handler(name, arguments)
                        result = await value if asyncio.iscoroutine(value) else value
                    else:
                        result = await self.runtime.call_tool(
                            name, arguments, consumer_plugin=consumer_plugin,
                        )
                        if result is None and tool_handler is None:
                            result = {'ok': False, 'error': '工具不可用'}
                        elif result is None:
                            value = tool_handler(name, arguments)
                            result = await value if asyncio.iscoroutine(value) else value
                    self.audit.tool_finish(run_id, tool_id, result=result)
                    return result
                except Exception as error:
                    self.audit.tool_finish(run_id, tool_id, error=str(error))
                    raise
                except asyncio.CancelledError:
                    self.audit.tool_finish(run_id, tool_id, error='interrupted')
                    raise

            required = list(dict.fromkeys(
                str(name or '').strip() for name in (required_tools or [])
                if str(name or '').strip()
            ))
            available = {
                str(item.get('function', {}).get('name') or '')
                for item in all_tools if isinstance(item, dict)
            }
            missing = [name for name in required if name not in available]
            if missing:
                raise AIServiceError('必要工具不可用：' + '、'.join(missing))
            schemas = {
                str(item.get('function', {}).get('name') or ''): item.get('function', {})
                for item in all_tools if isinstance(item, dict)
            }
            needs_arguments = [
                name for name in required
                if (schemas.get(name, {}).get('parameters') or {}).get('required')
            ]
            if needs_arguments:
                raise AIServiceError(
                    '必要工具不能无参数预执行：' + '、'.join(needs_arguments)
                )
            if required:
                evidence_messages = []
                for index, name in enumerate(required):
                    call_id = f'required_{run_id}_{index}'
                    result = await combined_handler(name, {})
                    if isinstance(result, dict) and (
                        result.get('ok') is False
                        or ('error' in result and len(result) == 1)
                    ):
                        detail = result.get('error') or '未知错误'
                        raise AIServiceError(f'必要工具 {name} 执行失败：{detail}')
                    evidence_messages.extend((
                        {
                            'role': 'assistant',
                            'content': '',
                            'tool_calls': [{
                                'id': call_id,
                                'type': 'function',
                                'function': {'name': name, 'arguments': '{}'},
                            }],
                        },
                        {
                            'role': 'tool',
                            'tool_call_id': call_id,
                            'name': name,
                            'content': json.dumps(
                                result, ensure_ascii=False, default=str,
                            )[:12000],
                        },
                    ))
                if prepared_messages:
                    prepared_messages = [
                        *prepared_messages[:-1], *evidence_messages, prepared_messages[-1],
                    ]
                else:
                    prepared_messages = evidence_messages

            last_error = None
            for provider, candidate_model in candidates:
                try:
                    result = await self._complete_candidate(
                        provider, candidate_model, prepared_messages, combined_prompt,
                        temperature, max_tokens, all_tools or None,
                        combined_handler if all_tools else None, max_tool_rounds,
                        run_id, completion_validator,
                    )
                    result['run_id'] = run_id
                    final_result = result
                    return result
                except (AIProviderError, aiohttp.ClientError, TimeoutError) as error:
                    last_error = error
                    self.audit.fail_running_attempt(run_id, str(error))
                    self._health[(provider['id'], candidate_model)] = {
                        'ok': False, 'error': str(error)[:160], 'checked_at': int(time.time()),
                    }
                    self.audit.event(run_id, 'failover', {
                        'provider_id': provider['id'], 'model': candidate_model, 'error': str(error),
                    })
                    if not self._config['auto_switch'] or model:
                        raise
            raise last_error or AIServiceError('所有接口均不可用')
        except asyncio.CancelledError:
            error_text = 'interrupted'
            self.audit.fail_running_attempt(run_id, error_text)
            raise
        except Exception as error:
            error_text = str(error)
            raise
        finally:
            self.audit.finish(
                run_id, response=final_result,
                usage=(final_result or {}).get('usage') or {}, error=error_text,
            )
            self.runtime.finish_run(run_id, error_text)

    async def run_agent(self, messages: list[dict], **kwargs) -> dict:
        """Run an explicit agent loop with central runtime capabilities enabled."""
        kwargs['enable_runtime_tools'] = True
        return await self.complete(messages, **kwargs)

    async def _complete_candidate(
        self, provider, model, messages, system_prompt, temperature,
        max_tokens, tools, tool_handler, max_tool_rounds, run_id='',
        completion_validator: CompletionValidator | None = None,
    ) -> dict:
        payload_messages = copy.deepcopy(messages)
        compatibility_prompt = _text_tool_protocol(tools)
        effective_prompt = '\n\n'.join(
            item for item in (system_prompt, compatibility_prompt) if item
        )
        if effective_prompt:
            payload_messages.insert(0, {'role': 'system', 'content': effective_prompt})
        payload = {
            'model': model,
            'messages': payload_messages,
            'temperature': self._config['temperature'] if temperature is None else temperature,
            'max_tokens': self._config['max_tokens'] if max_tokens is None else max_tokens,
        }
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'required' if completion_validator is not None else 'auto'
        rounds = self._config['max_tool_rounds'] if max_tool_rounds is None else max_tool_rounds
        tool_events = []
        for _round in range(max(1, rounds + 1)):
            try:
                data = await self._request(provider, payload, run_id)
            except AIServiceError as error:
                error_text = str(error).lower()
                if (
                    payload.get('tool_choice') == 'required'
                    and 'tool_choice' in error_text
                ):
                    # Some OpenAI-compatible gateways only accept "auto". The
                    # completion validator still prevents a prose-only success.
                    payload['tool_choice'] = 'auto'
                    data = await self._request(provider, payload, run_id)
                elif 'tools' in payload and _tools_parameter_unsupported(error_text):
                    # Keep the local schemas for XML parsing, but stop sending native
                    # function-calling fields to endpoints that reject them.
                    payload.pop('tools', None)
                    payload.pop('tool_choice', None)
                    data = await self._request(provider, payload, run_id)
                elif 'max_tokens' in error_text and 'max_completion_tokens' not in payload:
                    payload['max_completion_tokens'] = payload.pop('max_tokens')
                    data = await self._request(provider, payload, run_id)
                else:
                    raise
            try:
                message = data['choices'][0]['message']
            except (KeyError, IndexError, TypeError) as error:
                raise AIProviderError('接口返回中没有 choices[0].message') from error
            tool_calls = message.get('tool_calls') or []
            fallback_content = message.get('content')
            text_protocol = False
            if not tool_calls and isinstance(fallback_content, str):
                tool_calls, fallback_content = _xml_tool_calls(fallback_content, tools)
                text_protocol = bool(tool_calls)
            if not tool_calls:
                content = message.get('content')
                if isinstance(content, list):
                    content = ''.join(str(part.get('text') or '') for part in content if isinstance(part, dict))
                text = str(content or '').strip()
                if not text:
                    raise AIProviderError('接口返回了空消息')
                validation_error = (
                    completion_validator(text, copy.deepcopy(tool_events))
                    if completion_validator is not None else None
                )
                if validation_error:
                    if not tools or tool_handler is None or _round >= rounds:
                        raise AIExecutionIncomplete(str(validation_error))
                    payload['messages'].extend((
                        {'role': 'assistant', 'content': text},
                        {
                            'role': 'user',
                            'content': (
                                '[执行校验未通过] ' + str(validation_error)
                                + '\n下一条消息只调用一个能够完成该步骤的工具。'
                                  '收到真实工具结果后再继续下一步，不要输出计划或伪造结果。'
                            ),
                        },
                    ))
                    payload['tool_choice'] = 'required'
                    continue
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
            if len(tool_calls) > 8:
                raise AIProviderError('接口单次返回的工具调用超过 8 个')
            if text_protocol:
                payload['messages'].append({
                    'role': 'assistant',
                    'content': fallback_content or '[请求执行工具]',
                })
            else:
                payload['messages'].append({
                    'role': 'assistant',
                    'content': fallback_content or '',
                    'tool_calls': tool_calls,
                })
            text_results = []
            for call in tool_calls:
                function = call.get('function') or {}
                function_name = str(function.get('name') or '')
                try:
                    arguments = json.loads(function.get('arguments') or '{}')
                except (json.JSONDecodeError, TypeError) as error:
                    arguments = {}
                    detail = error.msg if isinstance(error, json.JSONDecodeError) else str(error)
                    result = {'ok': False, 'error': f'工具参数不是合法 JSON：{detail}'}
                else:
                    if not isinstance(arguments, dict):
                        result = {'ok': False, 'error': '工具参数必须是 JSON 对象'}
                    else:
                        result = tool_handler(function_name, arguments)
                        if asyncio.iscoroutine(result):
                            result = await result
                tool_events.append({
                    'name': function_name,
                    'arguments': copy.deepcopy(arguments),
                    'result': copy.deepcopy(result),
                })
                serialized = json.dumps(result, ensure_ascii=False, default=str)[:12000]
                if text_protocol:
                    text_results.append(f'[工具 {function_name} 执行结果]\n{serialized}')
                else:
                    payload['messages'].append({
                        'role': 'tool',
                        'tool_call_id': str(call.get('id') or ''),
                        'name': function_name,
                        'content': serialized,
                    })
            if text_protocol:
                payload['messages'].append({
                    'role': 'user',
                    'content': (
                        '\n\n'.join(text_results)
                        + '\n请基于真实结果继续；需要更多信息时继续使用同一 XML 工具协议。'
                    ),
                })
            if 'tools' in payload:
                payload['tool_choice'] = 'auto'
        raise AIServiceError('达到工具轮数上限')
