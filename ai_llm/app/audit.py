"""记录并脱敏模型调用日志。"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from collections import deque


_SENSITIVE_KEY = re.compile(
    r'(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|cookie|secret|password)',
    re.IGNORECASE,
)
_BEARER = re.compile(r'(?i)bearer\s+[a-z0-9._~+\-/=]{8,}')
_INLINE_SECRET = re.compile(
    r'(?i)(?P<label>(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)'
    r'\s*[:=]\s*)[^\s,;\"\']{6,}'
)
_PRIVATE_VALUE = re.compile(
    r'(?<![\w.])(?:127(?:\.\d{1,3}){3}|10(?:\.\d{1,3}){3}|'
    r'192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|'
    r'169\.254(?:\.\d{1,3}){2}|localhost)(?![\w.])',
    re.IGNORECASE,
)
_MAX_TEXT = 50000
_MAX_RECORDS = 500
_CONTENT_KEYS = {
    'content', 'messages', 'system_prompt', 'runtime_prompt', 'prompt', 'request',
    'response', 'arguments', 'result', 'body', 'code',
}


def _redact_text(value: str) -> str:
    text = _BEARER.sub('Bearer ********', str(value))
    text = _INLINE_SECRET.sub(lambda match: match.group('label') + '********', text)
    return _PRIVATE_VALUE.sub('[private-address]', text)


def _safe(value, depth: int = 0, *, include_content: bool = False, key: str = ''):
    if depth > 10:
        return '[depth limit]'
    if isinstance(value, dict):
        return {
            str(item_key): (
                '********' if _SENSITIVE_KEY.search(str(item_key))
                else '[content omitted]' if not include_content and str(item_key).casefold() in _CONTENT_KEYS
                else _safe(item, depth + 1, include_content=include_content, key=str(item_key))
            )
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1, include_content=include_content, key=key) for item in value[:500]]
    if isinstance(value, str):
        if not include_content and key.casefold() in _CONTENT_KEYS:
            return '[content omitted]'
        text = _redact_text(value)
        return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + '\n...[truncated]'
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _safe(str(value), depth + 1, include_content=include_content, key=key)


class InvocationAudit:
    def __init__(self, data_dir: str, *, include_content: bool = False):
        self._path = os.path.join(data_dir, 'invocation_logs.json')
        self._records: deque[dict] = deque(maxlen=_MAX_RECORDS)
        self._include_content = bool(include_content)
        self._load()

    def set_include_content(self, enabled: bool) -> None:
        self._include_content = bool(enabled)

    def _safe(self, value):
        return _safe(value, include_content=self._include_content)

    def _load(self):
        try:
            with open(self._path, encoding='utf-8') as file:
                values = json.load(file)
            if isinstance(values, list):
                self._records.extend(
                    _safe(item, include_content=self._include_content)
                    for item in values[-_MAX_RECORDS:] if isinstance(item, dict)
                )
                self._save()
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            temporary = self._path + '.tmp'
            with open(temporary, 'w', encoding='utf-8') as file:
                json.dump(list(self._records), file, ensure_ascii=False, separators=(',', ':'))
            os.replace(temporary, self._path)
        except OSError:
            pass

    def _find(self, run_id: str) -> dict | None:
        return next((item for item in reversed(self._records) if item.get('run_id') == run_id), None)

    def start(self, run_id: str, *, kind: str, session_id: str, consumer_plugin: str, request: dict):
        now = time.time()
        self._records.append({
            'run_id': run_id, 'kind': kind, 'status': 'running',
            'session_id': str(session_id or ''),
            'consumer_plugin': str(consumer_plugin or ''),
            'started_at': now, 'finished_at': None, 'duration_ms': None,
            'ttfb_ms': None, 'tokens_per_second': None,
            'request': self._safe(request), 'response': None, 'usage': {},
            'attempts': [], 'tools': [], 'events': [], 'error': '',
        })
        self._save()

    def event(self, run_id: str, event_type: str, data: dict | None = None):
        record = self._find(run_id)
        if record is None:
            return
        record['events'].append({'time': time.time(), 'type': event_type, 'data': self._safe(data or {})})
        record['events'] = record['events'][-200:]
        self._save()

    def attempt_start(self, run_id: str, provider: dict, model: str, payload: dict) -> str:
        record = self._find(run_id)
        attempt_id = f"{run_id}:{len(record.get('attempts', [])) + 1 if record else 1}"
        if record is not None:
            record['attempts'].append({
                'id': attempt_id, 'provider_id': provider.get('id', ''),
                'provider_name': provider.get('name', ''), 'model': model,
                'endpoint': str(provider.get('base_url') or '').rstrip('/') + '/chat/completions',
                'request_headers': self._safe({
                    'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + str(provider.get('api_key') or ''),
                }),
                'response_headers': {},
                'started_at': time.time(), 'duration_ms': None, 'ttfb_ms': None,
                'http_status': None, 'request_bytes': len(json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')),
                'response_bytes': None, 'request': self._safe(payload), 'response': None,
                'usage': {}, 'status': 'running', 'error': '',
            })
            self._save()
        return attempt_id

    def attempt_finish(self, run_id: str, attempt_id: str, *, status: str, http_status: int | None,
                       response=None, usage: dict | None = None, error: str = '',
                       ttfb_ms: int | None = None, response_headers: dict | None = None):
        record = self._find(run_id)
        if record is None:
            return
        attempt = next((item for item in record['attempts'] if item.get('id') == attempt_id), None)
        if attempt is None:
            return
        attempt['duration_ms'] = round((time.time() - attempt['started_at']) * 1000)
        attempt['ttfb_ms'] = ttfb_ms
        attempt['http_status'] = http_status
        attempt['response_headers'] = self._safe(response_headers or {})
        attempt['response'] = self._safe(response)
        attempt['response_bytes'] = len(json.dumps(response, ensure_ascii=False, default=str).encode('utf-8')) if response is not None else 0
        attempt['usage'] = self._safe(usage or {})
        completion_tokens = int(
            (usage or {}).get('completion_tokens')
            or (usage or {}).get('output_tokens')
            or 0
        )
        generation_ms = attempt['duration_ms'] - int(ttfb_ms or 0)
        attempt['tokens_per_second'] = (
            round(completion_tokens / (generation_ms / 1000), 2)
            if completion_tokens and generation_ms > 0 else None
        )
        attempt['status'] = status
        attempt['error'] = _redact_text(str(error or ''))[:2000]
        if record.get('ttfb_ms') is None and ttfb_ms is not None:
            record['ttfb_ms'] = ttfb_ms
        self._save()

    def fail_running_attempt(self, run_id: str, error: str):
        record = self._find(run_id)
        if record is None:
            return
        attempt = next((item for item in reversed(record['attempts']) if item.get('status') == 'running'), None)
        if attempt is not None:
            self.attempt_finish(
                run_id, attempt['id'], status='error', http_status=attempt.get('http_status'),
                error=error,
            )

    def tool_start(self, run_id: str, name: str, arguments: dict) -> str:
        record = self._find(run_id)
        tool_id = f"tool-{len(record.get('tools', [])) + 1 if record else 1}"
        if record is not None:
            record['tools'].append({
                'id': tool_id, 'name': str(name), 'arguments': self._safe(arguments),
                'started_at': time.time(), 'duration_ms': None, 'status': 'running',
                'result': None, 'error': '',
            })
            self._save()
        return tool_id

    def tool_finish(self, run_id: str, tool_id: str, *, result=None, error: str = ''):
        record = self._find(run_id)
        if record is None:
            return
        tool = next((item for item in record['tools'] if item.get('id') == tool_id), None)
        if tool is None:
            return
        tool['duration_ms'] = round((time.time() - tool['started_at']) * 1000)
        tool['status'] = 'error' if error else 'success'
        tool['result'] = self._safe(result)
        tool['error'] = _redact_text(str(error or ''))[:2000]
        self._save()

    def finish(self, run_id: str, *, response=None, usage: dict | None = None, error: str = ''):
        record = self._find(run_id)
        if record is None:
            return
        record['finished_at'] = time.time()
        record['duration_ms'] = round((record['finished_at'] - record['started_at']) * 1000)
        record['status'] = 'error' if error else 'success'
        record['response'] = self._safe(response)
        record['usage'] = self._safe(usage or {})
        record['error'] = _redact_text(str(error or ''))[:4000]
        completion_tokens = int((usage or {}).get('completion_tokens') or (usage or {}).get('output_tokens') or 0)
        generation_ms = record['duration_ms'] - int(record.get('ttfb_ms') or 0)
        if completion_tokens and generation_ms > 0:
            record['tokens_per_second'] = round(completion_tokens / (generation_ms / 1000), 2)
        self._save()

    def page(
        self, *, page: int = 1, page_size: int = 20,
        status: str = '', provider: str = '', search: str = '',
    ) -> dict:
        """返回过滤后的日志摘要分页。"""
        values = list(reversed(self._records))
        if status:
            values = [item for item in values if item.get('status') == status]
        if provider:
            values = [item for item in values if any(
                provider in {attempt.get('provider_id'), attempt.get('provider_name')}
                for attempt in item.get('attempts', [])
            )]
        if search:
            needle = search.lower()
            values = [item for item in values if needle in json.dumps(item, ensure_ascii=False).lower()]
        size = max(1, min(int(page_size), 100))
        total = len(values)
        pages = max(1, (total + size - 1) // size)
        current = max(1, min(int(page), pages))
        start = (current - 1) * size
        return {
            'items': [self._summary(item) for item in values[start:start + size]],
            'page': current,
            'page_size': size,
            'pages': pages,
            'total': total,
        }

    def list(self, *, limit: int = 100, status: str = '', provider: str = '', search: str = '') -> list[dict]:
        """兼容旧版的限量日志列表。"""
        return self.page(
            page=1, page_size=limit, status=status, provider=provider, search=search,
        )['items']

    def get(self, run_id: str) -> dict | None:
        value = self._find(run_id)
        return copy.deepcopy(value) if value else None

    def clear(self):
        self._records.clear()
        self._save()

    def stats(self) -> dict:
        completed = [item for item in self._records if item.get('duration_ms') is not None]
        successful = [item for item in completed if item.get('status') == 'success']
        return {
            'total': len(self._records), 'running': sum(item.get('status') == 'running' for item in self._records),
            'success_rate': round(len(successful) * 100 / len(completed), 1) if completed else 0,
            'average_duration_ms': round(sum(item['duration_ms'] for item in completed) / len(completed)) if completed else 0,
            'total_tokens': sum(int((item.get('usage') or {}).get('total_tokens') or 0) for item in completed),
        }

    @staticmethod
    def _summary(item: dict) -> dict:
        attempts = item.get('attempts', [])
        last = attempts[-1] if attempts else {}
        return {key: copy.deepcopy(item.get(key)) for key in (
            'run_id', 'kind', 'status', 'session_id', 'consumer_plugin', 'started_at',
            'finished_at', 'duration_ms', 'ttfb_ms', 'tokens_per_second', 'usage', 'error',
        )} | {
            'provider_id': last.get('provider_id', ''), 'provider_name': last.get('provider_name', ''),
            'model': last.get('model', ''), 'attempt_count': len(attempts),
            'tool_count': len(item.get('tools', [])),
            'endpoint': last.get('endpoint', ''), 'http_status': last.get('http_status'),
            'request_bytes': last.get('request_bytes', 0),
            'response_bytes': last.get('response_bytes', 0),
        }
