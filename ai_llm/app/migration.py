"""迁移旧版 ai_dev 接口配置。"""
from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable, Mapping

from .service import DEFAULT_CONFIG, normalize_config


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as file:
            value = json.load(file)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _model_data(raw: dict, fallback: str) -> tuple[list[str], list[str]]:
    entries = raw.get('models') if isinstance(raw.get('models'), list) else []
    valid = [item for item in entries if isinstance(item, dict) and str(item.get('name') or '').strip()]

    def priority(item: dict) -> int:
        try:
            return int(item.get('priority') or 0)
        except (TypeError, ValueError):
            return 0

    valid.sort(key=priority)
    models = list(dict.fromkeys(str(item['name']).strip() for item in valid))
    if fallback and fallback not in models:
        models.insert(0, fallback)
    if not models:
        models = [fallback or DEFAULT_CONFIG['providers'][0]['model']]
    disabled = [str(item['name']).strip() for item in valid if not item.get('enabled', True)]
    return models, disabled


def load_ai_dev_config(
    bot_root: str,
    settings_get: Callable[[str, object], object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict | None:
    """从 ai_dev 文件生成中央配置。"""
    plugin_dir = next(
        (
            path for path in (
                os.path.join(bot_root, 'plugins', 'ai_dev'),
                os.path.join(bot_root, 'ai_dev'),
            )
            if os.path.isdir(path)
        ),
        '',
    )
    runtime = _read_json(os.path.join(plugin_dir, 'data', 'runtime_config.json')) if plugin_dir else {}
    presets_data = _read_json(os.path.join(plugin_dir, 'data', 'ai_presets.json')) if plugin_dir else {}
    presets = presets_data.get('presets') if isinstance(presets_data.get('presets'), list) else []
    env = environ if environ is not None else os.environ

    def setting(key: str, default=''):
        try:
            return settings_get(key, default) if settings_get else default
        except Exception:
            return default

    settings = {key: setting(key, '') for key in ('base_url', 'api_key', 'model')}
    api_key = runtime.get('api_key') or settings['api_key'] or env.get('AI_DEV_API_KEY') or env.get('OPENAI_API_KEY') or ''
    found = bool(runtime or presets or api_key or any(settings.values()))
    if not found:
        return None

    default = DEFAULT_CONFIG['providers'][0]
    base_url = str(runtime.get('base_url') or settings['base_url'] or default['base_url'])
    model = str(runtime.get('model') or settings['model'] or default['model'])
    providers = [{
        **copy.deepcopy(default),
        'name': 'AI 开发工具（当前）',
        'base_url': base_url,
        'api_key': str(api_key),
        'model': model,
        'models': [model],
        'model_priority': [model],
        'priority': 1000,
    }]
    active_id = str(presets_data.get('active_id') or '')
    for index, raw in enumerate(presets):
        if not isinstance(raw, dict) or not raw.get('id'):
            continue
        preset_model = str(raw.get('model') or model).strip()
        models, disabled = _model_data(raw, preset_model)
        provider_id = f"ai-dev-{str(raw['id']).strip()}"
        providers.append({
            'id': provider_id,
            'name': str(raw.get('name') or 'AI 开发工具接口').strip(),
            'base_url': str(raw.get('base_url') or base_url).strip(),
            'api_key': str(raw.get('api_key') or api_key),
            'model': preset_model or models[0],
            'models': models,
            'model_priority': models,
            'disabled_models': disabled,
            'model_priority_enabled': True,
            'priority': max(1, 900 - index),
            'enabled': True,
            'builtin': False,
        })
        if str(raw['id']) == active_id:
            providers[0]['priority'] = 899
            providers[-1]['priority'] = 1000

    result = copy.deepcopy(DEFAULT_CONFIG)
    result['providers'] = providers
    active = next((item for item in providers if item['priority'] == 1000), providers[0])
    result['active_provider'] = active['id']
    return normalize_config(result)
