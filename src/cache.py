"""Shared public-data cache. Authentication and database writes never use it."""
import asyncio
import hashlib
import json
import random
import time
import uuid


class BindingBackend:
    def __init__(self, binding):
        self.binding = binding

    async def invoke(self, op, **data):
        from js import Request, Object
        from pyodide.ffi import to_js
        options = to_js({'method': 'POST', 'headers': {'Content-Type': 'application/json'},
                         'body': json.dumps(dict(op=op, **data))}, dict_converter=Object.fromEntries)
        response = await self.binding.fetch(Request.new('https://cache.internal/', options))
        if response.status != 200:
            raise OSError('Cache unavailable')
        return json.loads(await response.text())['value']


class SharedCache:
    def __init__(self, backend=None):
        self.backend = backend
        self.unavailable_until = 0
        self.memory = {}
        self.locks = {}
        self.generation, self.generation_until = 'initial', 0

    async def invoke(self, op, **data):
        if self.backend is None or time.monotonic() < self.unavailable_until:
            raise OSError('Cache unavailable')
        try:
            return await asyncio.wait_for(self.backend.invoke(op, **data), timeout=2)
        except Exception:
            self.unavailable_until = time.monotonic() + 30
            # A safe diagnostic without key, value, username or password.
            print(json.dumps({'event': 'cache_unavailable', 'fallback': 'firestore'}))
            raise OSError('Cache unavailable') from None

    async def version(self):
        if time.monotonic() >= self.generation_until:
            try:
                self.generation = await self.invoke('get', key='generation') or 'initial'
            except OSError:
                pass
            self.generation_until = time.monotonic() + 5
        return self.generation

    async def invalidate(self):
        await self.invoke('invalidate')
        self.generation_until = 0
        self.memory.clear()

    def remember(self, key, value, ttl):
        if len(self.memory) >= 128:
            self.memory.pop(next(iter(self.memory)))
        self.memory[key] = time.monotonic() + ttl, value

    async def read(self, key):
        started = time.monotonic()
        entry = await self.invoke('read', key=key)
        if entry['value'] is None:
            return None
        value = json.loads(entry['value'])
        remaining = max(0, entry['ttl_ms'] / 1000 - (time.monotonic() - started))
        self.remember(key, value, min(5, remaining))
        return value

    async def check_quota(self):
        # Brief shared backoff prevents an empty cache from hammering exhausted Firestore.
        if await self.invoke('get', key='firestore-quota-backoff'):
            from services import ServiceError
            raise ServiceError('CATALOG_QUOTA_EXCEEDED', 503, 30)

    async def load(self, loader):
        from services import ServiceError
        try:
            return await loader()
        except ServiceError as error:
            if error.code == 'CATALOG_QUOTA_EXCEEDED':
                try:
                    await self.invoke('set', key='firestore-quota-backoff', value='1', ttl=30)
                except OSError:
                    pass
            raise

    async def get_or_load(self, name, ttl, loader):
        generation = await self.version()
        key = generation + ':' + hashlib.sha256(name.encode()).hexdigest()
        lock = self.locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                entry = self.memory.get(key)
                if entry and entry[0] > time.monotonic():
                    return entry[1]
                try:
                    value = await self.read(key)
                    if value is not None:
                        return value
                    await self.check_quota()
                    owner = uuid.uuid4().hex
                    lock_key = 'lock:' + key
                    claimed = await self.invoke('claim', key=lock_key, value=owner, ttl=30)
                    deadline = time.monotonic() + 32
                    while not claimed:
                        await asyncio.sleep(random.uniform(0.3, 0.6))
                        value = await self.read(key)
                        if value is not None:
                            return value
                        await self.check_quota()
                        if time.monotonic() >= deadline:
                            from services import ServiceError
                            raise ServiceError('CATALOG_BUSY', 503, 5)
                        claimed = await self.invoke('claim', key=lock_key, value=owner, ttl=30)
                except (OSError, ValueError, TypeError):
                    # Redis failure never stops a working Firebase read.
                    value = await self.load(loader)
                    self.remember(key, value, min(ttl, 30))
                    return value
                try:
                    value = await self.load(loader)
                    started = time.monotonic()
                    try:
                        await self.invoke('set', key=key, value=json.dumps(value, separators=(',', ':')), ttl=ttl)
                    except OSError:
                        pass
                    self.remember(key, value, max(0, min(5, ttl - (time.monotonic() - started))))
                    return value
                finally:
                    try:
                        await self.invoke('release', key=lock_key, value=owner)
                    except OSError:
                        pass
        finally:
            # Waiting tasks retain their lock object; avoid unbounded product-id state.
            if not lock.locked() and not getattr(lock, '_waiters', None):
                self.locks.pop(key, None)
