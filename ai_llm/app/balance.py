"""Best-effort relay balance discovery; no model requests or privileged credentials."""
from __future__ import annotations

import asyncio
import hashlib
import math
import time

import aiohttp


class BalanceReader:
    def __init__(self):
        self._cache = {}
        self._locks = {}
        self._limit = asyncio.Semaphore(4)

    async def read(self, provider: dict, *, refresh: bool = False) -> dict:
        if (not provider.get('enabled') or not provider.get('balance_enabled', True)
                or provider.get('api_type') not in {'openai_compatible', 'agnes', 'jimeng2api', 'z_image_gitee'}
                or not provider.get('api_key')):
            return {'available': False}
        base = str(provider.get('base_url') or '').rstrip('/')
        for suffix in ('/chat/completions', '/responses'):
            if base.endswith(suffix):
                base = base[:-len(suffix)]
        key = hashlib.sha256((base + '\0' + provider['api_key']).encode()).hexdigest()
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if cached and not refresh and time.monotonic() < cached[0]:
                return dict(cached[1])
            async with self._limit:
                result = await self._fetch(base, provider['api_key'])
            self._cache[key] = (time.monotonic() + (300 if result.get('available') else 3600), result)
            if len(self._cache) > 200:
                oldest = next(iter(self._cache))
                self._cache.pop(oldest, None)
                self._locks.pop(oldest, None)
            return dict(result)

    async def _fetch(self, base: str, api_key: str) -> dict:
        # New API / One API billing convention. Unsupported/auth failures are silent.
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                async def get(url):
                    async with session.get(url, headers={'Authorization': f'Bearer {api_key}'}, allow_redirects=False) as response:
                        if response.status != 200:
                            return None
                        data = await response.json(content_type=None)
                        return data if isinstance(data, dict) else None

                subscription = await get(base + '/dashboard/billing/subscription')
                if not subscription or 'hard_limit_usd' not in subscription:
                    return {'available': False}
                usage = await get(base + '/dashboard/billing/usage')
                if not usage or 'total_usage' not in usage:
                    return {'available': False}
                total = float(subscription['hard_limit_usd'])
                used = float(usage['total_usage']) / 100
                if not all(math.isfinite(value) and value >= 0 for value in (total, used)):
                    return {'available': False}
                return {'available': True, 'remaining': round(total - used, 6), 'currency': 'USD',
                        'label': '接口额度余额', 'checked_at': int(time.time())}
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, KeyError):
            return {'available': False}
