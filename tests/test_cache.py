import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch
from cache import SharedCache
from services import FirebaseService, HTTPResult, ServiceError
from fastapi.testclient import TestClient
from main import app


class MemoryRedis:
    def __init__(self): self.data = {}
    async def invoke(self, op, key='', value=None, ttl=30):
        if op == 'invalidate': self.data['generation']=(time.monotonic()+3600,'new-generation'); return True
        entry = self.data.get(key)
        previous = entry[1] if entry and entry[0] > time.monotonic() else None
        if op == 'get': return previous
        if op == 'read': return {'value':previous,'ttl_ms':max(0,(entry[0]-time.monotonic())*1000) if previous is not None else -2}
        if op == 'claim' and previous is not None: return False
        if op in ('set','claim'): self.data[key]=(time.monotonic()+ttl,value); return True
        if op == 'release' and previous == value: self.data.pop(key,None); return True
        return False


class CacheEndpointTest(unittest.TestCase):
    def test_players_cannot_invalidate_and_admin_is_checked_before_purge(self):
        firebase=type('Firebase',(),{'authenticate_admin':AsyncMock(side_effect=ServiceError('ADMIN_REQUIRED',403)),
                                     'cache':type('Cache',(),{'invalidate':AsyncMock()})()})()
        with patch('services.cached_firebase',return_value=firebase), TestClient(app) as client:
            self.assertEqual(client.post('/v1/cache/invalidate').status_code,401)
            response=client.post('/v1/cache/invalidate',headers={'Authorization':'Bearer '+'t'*30})
            self.assertEqual(response.status_code,403)
            firebase.cache.invalidate.assert_not_called()
            firebase.authenticate_admin.side_effect=None
            self.assertEqual(client.post('/v1/cache/invalidate',headers={'Authorization':'Bearer '+'t'*30}).status_code,200)
            firebase.cache.invalidate.assert_awaited_once()


class CacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_memory_reuses_catalog_beyond_five_seconds_but_rechecks_generation(self):
        clock=[100.0]
        backend=MemoryRedis();cache=SharedCache(backend);loader=AsyncMock(return_value=['old'])
        with patch('cache.time.monotonic',side_effect=lambda:clock[0]),patch.object(backend,'invoke',wraps=backend.invoke) as calls:
            await cache.get_or_load('facet:photos',900,loader)
            reads=sum(call.args[0]=='read' for call in calls.call_args_list)
            clock[0]+=8
            self.assertEqual(await cache.get_or_load('facet:photos',900,loader),['old'])
            self.assertEqual(sum(call.args[0]=='read' for call in calls.call_args_list),reads)
            await backend.invoke('invalidate');clock[0]+=6
            loader.return_value=['new']
            self.assertEqual(await cache.get_or_load('facet:photos',900,loader),['new'])
        self.assertEqual(loader.call_count,2)

    async def test_shared_quota_backoff_keeps_warm_data_but_stops_new_reads(self):
        backend=MemoryRedis()
        first=SharedCache(backend)
        await first.get_or_load('product:p',60,AsyncMock(return_value={'nome':'cached'}))
        failing=AsyncMock(side_effect=ServiceError('CATALOG_QUOTA_EXCEEDED',503,3600))
        with self.assertRaises(ServiceError): await first.get_or_load('facet:a',900,failing)
        with self.assertRaises(ServiceError): await SharedCache(backend).get_or_load('facet:b',900,failing)
        self.assertEqual(failing.call_count,1)
        self.assertEqual(await SharedCache(backend).get_or_load('product:p',60,failing),{'nome':'cached'})

    async def test_local_memory_never_extends_redis_expiry(self):
        backend=MemoryRedis();first=SharedCache(backend)
        loader=AsyncMock(side_effect=[['old'],['new']])
        await first.get_or_load('facet:a',1,loader)
        await asyncio.sleep(.9)
        second=SharedCache(backend)
        self.assertEqual(await second.get_or_load('facet:a',1,loader),['old'])
        await asyncio.sleep(.15)
        self.assertEqual(await second.get_or_load('facet:a',1,loader),['new'])
    async def test_simultaneous_instances_load_once_and_share_the_result(self):
        backend=MemoryRedis()
        async def read():
            await asyncio.sleep(.05)
            return {'nome':'Produto público'}
        loader=AsyncMock(side_effect=read)
        caches=[SharedCache(backend) for _ in range(30)]
        results=await asyncio.gather(*(cache.get_or_load('product:p',60,loader) for cache in caches))
        self.assertEqual(loader.call_count,1)
        self.assertTrue(all(r==results[0] for r in results))
        self.assertEqual(await SharedCache(backend).get_or_load('product:p',60,loader),results[0])
        self.assertEqual(loader.call_count,1)

    async def test_redis_outage_falls_back_to_firebase_and_circuits_repeated_attempts(self):
        backend=type('Broken',(),{'invoke':AsyncMock(side_effect=OSError('secret'))})()
        cache=SharedCache(backend);loader=AsyncMock(return_value=['real'])
        self.assertEqual(await cache.get_or_load('facet:a',900,loader),['real'])
        self.assertEqual(await cache.get_or_load('facet:a',900,loader),['real'])
        self.assertEqual(loader.call_count,1);self.assertEqual(backend.invoke.call_count,1)

    async def test_invalidation_reloads_in_other_instance_and_old_loader_cannot_overwrite(self):
        backend=MemoryRedis();first,second=SharedCache(backend),SharedCache(backend)
        loader=AsyncMock(side_effect=[['old'],['new']])
        await first.get_or_load('facet:a',900,loader)
        self.assertEqual(await second.get_or_load('facet:a',900,loader),['old'])
        await first.invalidate();second.generation_until=0
        self.assertEqual(await second.get_or_load('facet:a',900,loader),['new'])

    async def test_expired_product_is_rechecked_and_unavailable_product_is_not_cached(self):
        backend=MemoryRedis();cache=SharedCache(backend)
        loader=AsyncMock(side_effect=[{'nome':'old'},ServiceError('PRODUCT_NOT_AVAILABLE',422)])
        await cache.get_or_load('product:p',60,loader)
        for key in list(backend.data):
            if key != 'generation': backend.data.pop(key,None)
        cache.memory.clear()
        with self.assertRaises(ServiceError): await cache.get_or_load('product:p',60,loader)
        self.assertFalse(any('PRODUCT_NOT_AVAILABLE' in str(v) for v in backend.data.values()))

    async def test_catalog_warm_in_a_new_instance_never_queries_firestore_or_caches_private_fields(self):
        backend=MemoryRedis()
        doc={'document':{'name':'projects/epav-game/databases/(default)/documents/produtos_swift/p',
                         'fields':{'nome':{'stringValue':'Produto'},'disponivelNoJogo':{'booleanValue':True},
                                   'dadosOriginais':{'mapValue':{'fields':{'Margem':{'doubleValue':99},
                                                                    'Nome Fornecedor':{'stringValue':'private'}}}}}}}
        transport=AsyncMock(return_value=HTTPResult(200,{},json.dumps([doc])))
        first=FirebaseService('public','{}',transport,SharedCache(backend))
        second=FirebaseService('public','{}',transport,SharedCache(backend))
        with patch.object(first,'access_token',AsyncMock(return_value='token')), patch.object(second,'access_token',AsyncMock()) as oauth:
            self.assertEqual(await first.catalog(),await second.catalog())
            self.assertEqual(transport.call_count,1);oauth.assert_not_called()
        self.assertNotIn('private',str(backend.data));self.assertNotIn('Margem',str(backend.data))

    async def test_ranking_caches_only_public_columns(self):
        doc={'document':{'fields':{'nome':{'stringValue':'Jogador'},'uid':{'stringValue':'private-uid'},
                                  'email':{'stringValue':'private-email'},'pontos':{'integerValue':'10'}}}}
        transport=AsyncMock(return_value=HTTPResult(200,{},json.dumps([doc])))
        service=FirebaseService('public','{}',transport,SharedCache(MemoryRedis()))
        with patch.object(service,'access_token',AsyncMock(return_value='token')):
            self.assertEqual(await service.ranking(),await service.ranking())
        self.assertEqual(transport.call_count,1)
        self.assertNotIn('private',str(service.cache.memory))

    async def test_admin_claim_is_required_and_checked_fresh(self):
        transport=AsyncMock(return_value=HTTPResult(200,{},json.dumps({'users':[{'localId':'u','customAttributes':'{}'}]})))
        service=FirebaseService('public','{}',transport)
        with self.assertRaises(ServiceError) as error: await service.authenticate_admin('token')
        self.assertEqual(error.exception.status,403)
        transport.return_value=HTTPResult(200,{},json.dumps({'users':[{'customAttributes':'{"admin":true}'}]}))
        await service.authenticate_admin('token')
        transport.return_value=HTTPResult(200,{},json.dumps({'users':[{'disabled':True,'customAttributes':'{"admin":true}'}]}))
        with self.assertRaises(ServiceError): await service.authenticate_admin('token')
