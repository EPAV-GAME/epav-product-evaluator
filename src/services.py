import asyncio
import base64
import hashlib
import json
import re
import sys
import time
import urllib.parse
import zlib
from dataclasses import dataclass
import httpx
from pydantic import ValidationError
from models import AIJudgement

class ServiceError(Exception):
    def __init__(self, code, status=503, retry_after=None):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)

@dataclass
class HTTPResult:
    status: int
    headers: dict
    body: str

    def json(self):
        return json.loads(self.body)

async def post_or_get(url, *, method='POST', headers=None, payload=None, form=None, max_response_chars=256_000):
    headers = dict(headers or {})
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(payload)
    elif form is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        body = urllib.parse.urlencode(form)
    else:
        body = None
    if sys.platform == 'emscripten':
        from workers import fetch
        # workerd accepts manual/follow redirect modes. Never follow redirects with credentials.
        response = await fetch(url, method=method, headers=headers, body=body, redirect='manual')
        text = await response.text()
        result = HTTPResult(response.status, {k.lower():v for k,v in response.headers.items()}, text)
    else:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            response = await client.request(method,url,headers=headers,content=body)
            result = HTTPResult(response.status_code,dict(response.headers),response.text)
    if len(result.body) > max_response_chars:
        raise ServiceError('UPSTREAM_RESPONSE_TOO_LARGE',502)
    return result

def parse_keys(raw):
    keys = list(dict.fromkeys(part.strip().strip('{}').strip() for part in raw.split('|') if part.strip()))
    if not keys or len(keys)>16 or any(not re.fullmatch(r'gsk_[A-Za-z0-9_-]{16,200}',key) for key in keys):
        raise ServiceError('GROQ_KEYS_NOT_CONFIGURED')
    return keys

class GroqPool:
    def __init__(self, raw, transport=post_or_get, clock=time.monotonic):
        self.keys = parse_keys(raw)
        self.transport, self.clock = transport, clock
        self.cooldown = {}

    async def complete(self,payload):
        invalid_output = False
        for index, key in enumerate(self.keys):
            if self.cooldown.get(index,0)>self.clock():
                continue
            try:
                response = await asyncio.wait_for(self.transport(
                    'https://api.groq.com/openai/v1/chat/completions',
                    headers={'Authorization':'Bearer '+key},payload=payload),timeout=12)
            except (TimeoutError, httpx.HTTPError, OSError):
                self.cooldown[index] = self.clock()+10
                continue
            if response.status in (429,402,401,403) or response.status>=500:
                try:
                    pause = min(3600,max(1,int(float(response.headers.get('retry-after','60')))))
                except ValueError:
                    pause = 60
                self.cooldown[index] = self.clock()+pause
                continue
            if response.status != 200:
                # Request/model errors cannot be solved by consuming more keys.
                try:
                    code=response.json().get('error',{}).get('code')
                except (ValueError,TypeError,AttributeError):
                    code=None
                safe_code=code if code in {'json_validate_failed','model_not_found','invalid_api_key','context_length_exceeded'} else 'request_rejected'
                print(json.dumps({'event':'groq_request_rejected','status':response.status,'code':safe_code}))
                raise ServiceError('GROQ_REQUEST_REJECTED',502)
            try:
                answer = response.json()['choices'][0]['message']['content']
                return AIJudgement.model_validate_json(answer)
            except (KeyError,IndexError,TypeError,ValueError,ValidationError):
                invalid_output = True
                break
        if invalid_output:
            raise ServiceError('INVALID_AI_RESPONSE',502)
        remaining = [deadline-self.clock() for deadline in self.cooldown.values() if deadline>self.clock()]
        retry = max(1,int(min(remaining))) if remaining else 60
        raise ServiceError('GROQ_TEMPORARILY_UNAVAILABLE',503,retry)

def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

async def sign_jwt(email,private_key):
    now = int(time.time())
    claims = dict(iss=email,scope='https://www.googleapis.com/auth/datastore',
                  aud='https://oauth2.googleapis.com/token',iat=now,exp=now+3600)
    message = (b64(b'{"alg":"RS256","typ":"JWT"}')+'.'+b64(json.dumps(claims).encode())).encode()
    if sys.platform == 'emscripten':
        from js import crypto, Object, Uint8Array
        from pyodide.ffi import to_js
        raw = base64.b64decode(''.join(line for line in private_key.splitlines() if not line.startswith('-----')))
        algorithm = to_js({'name':'RSASSA-PKCS1-v1_5','hash':'SHA-256'},dict_converter=Object.fromEntries)
        key = await crypto.subtle.importKey('pkcs8',to_js(memoryview(raw)),algorithm,False,to_js(['sign']))
        buffer = await crypto.subtle.sign('RSASSA-PKCS1-v1_5',key,to_js(memoryview(message)))
        signature = bytes(Uint8Array.new(buffer).to_py())
    else:
        from cryptography.hazmat.primitives import hashes,serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_private_key(private_key.encode(),password=None)
        signature = key.sign(message,padding.PKCS1v15(),hashes.SHA256())
    return message.decode()+'.'+b64(signature)

def decode(value):
    if 'mapValue' in value:
        return {k:decode(v) for k,v in value['mapValue'].get('fields',{}).items()}
    if 'arrayValue' in value:
        return [decode(v) for v in value['arrayValue'].get('values',[])]
    if 'integerValue' in value:
        return int(value['integerValue'])
    return next(iter(value.values()))

class FirebaseService:
    def __init__(self,web_key,account_raw,transport=post_or_get,cache=None):
        self.web_key,self.transport = web_key,transport
        self.account_raw = account_raw
        self.token,self.expires = '',0
        self.token_lock = asyncio.Lock()
        from cache import SharedCache
        self.cache = cache or SharedCache()

    async def authenticate(self,id_token):
        response = await self.transport('https://identitytoolkit.googleapis.com/v1/accounts:lookup?key='+self.web_key,payload={'idToken':id_token})
        if response.status in (400,401,403):
            raise ServiceError('AUTH_INVALID',401)
        if response.status != 200:
            raise ServiceError('AUTH_UNAVAILABLE')
        try:
            users = response.json()['users']
            if len(users)!=1 or users[0].get('disabled'):
                raise ServiceError('AUTH_INVALID',401)
            return users[0]['localId']
        except (ValueError,KeyError,IndexError,TypeError):
            raise ServiceError('AUTH_INVALID',401) from None

    async def access_token(self):
        async with self.token_lock:
            return await self._access_token()

    async def _access_token(self):
        if time.time()<self.expires-60:
            return self.token
        try:
            account = json.loads(self.account_raw)
            if account.get('project_id')!='epav-game':
                raise ValueError('Wrong project')
            assertion = await sign_jwt(account['client_email'],account['private_key'])
            response = await self.transport('https://oauth2.googleapis.com/token',form={
                'grant_type':'urn:ietf:params:oauth:grant-type:jwt-bearer','assertion':assertion})
            if response.status!=200:
                raise ValueError('OAuth failed')
            result = response.json()
            self.token,self.expires = result['access_token'],time.time()+int(result['expires_in'])
            return self.token
        except (ValueError,KeyError,TypeError):
            raise ServiceError('CATALOG_CREDENTIALS_NOT_CONFIGURED') from None

    async def product(self,doc_id):
        return await self.cache.get_or_load("product:"+doc_id, 60, lambda: self._product(doc_id))

    async def menu_product(self,doc_id):
        # The recommendation pool already contains the public fiche. Both game
        # editions reuse it instead of reading each selected/previous food again.
        for identifier,data in await self._menu_catalog():
            if identifier==doc_id:
                from recommendations import public_product
                card=public_product(identifier,data)
                return {key:card[key] for key in ('id','nome','tiposProduto','ocasioes',
                    'marca','formato','unidade_medida','peso_embalagem_kg')}
        raise ServiceError('PRODUCT_NOT_AVAILABLE',422)

    async def _product(self,doc_id):
        token = await self.access_token()
        url = 'https://firestore.googleapis.com/v1/projects/epav-game/databases/(default)/documents/produtos_swift/'+urllib.parse.quote(doc_id,safe='')
        response = await self.transport(url,method='GET',headers={'Authorization':'Bearer '+token})
        if response.status==404:
            raise ServiceError('PRODUCT_NOT_FOUND',404)
        if response.status==429:
            raise ServiceError('CATALOG_QUOTA_EXCEEDED',503,3600)
        if response.status!=200:
            raise ServiceError('CATALOG_UNAVAILABLE')
        data = {k:decode(v) for k,v in response.json().get('fields',{}).items()}
        if data.get('disponivelNoJogo') is not True:
            raise ServiceError('PRODUCT_NOT_AVAILABLE',422)
        original = data.get('dadosOriginais',{})
        from recommendations import package_weight
        # Do not send margins, sales figures, suppliers or the player's identity to Groq.
        return dict(id=doc_id,nome=data['nome'],tiposProduto=data.get('tiposProduto',[]),
                    ocasioes=data.get('ocasioes',[]),marca=original.get('Marca'),
                    formato=original.get('Formato'),unidade_medida=original.get('Unidade Medida'),
                    peso_embalagem_kg=package_weight(data['nome']))

    async def catalog(self,context=None):
        if context and context.get('categoria_refeicao'):
            return await self._menu_catalog()
        from recommendations import product_parameters, PRODUCT_PARAMETERS, normalize
        occasion = product_parameters(context)['ocasiao'] if context is not None else 'Dia a dia'
        # The occasion query already returns all product types; no extra type scans.
        facets = [occasion]
        fields = ['nome','codigo','disponivelNoJogo','tiposProduto','ocasioes','imagemSwift.url',
                  'dadosOriginais.Marca','dadosOriginais.Formato','dadosOriginais.Unidade Medida']
        async def query_facet(facet):
            token = await self.access_token()
            query = {'from':[{'collectionId':'produtos_swift'}],
                 'select':{'fields':[{'fieldPath':'.'.join('`'+part+'`' for part in field.split('.'))} for field in fields]},
                 'where':{'compositeFilter':{'op':'AND','filters':[
                     {'fieldFilter':{'field':{'fieldPath':'disponivelNoJogo'},'op':'EQUAL','value':{'booleanValue':True}}},
                     {'fieldFilter':{'field':{'fieldPath':'`dadosOriginais`.`'+facet+'`'},'op':'EQUAL','value':{'stringValue':'SIM'}}}]}},
                 'orderBy':[{'field':{'fieldPath':'__name__'},'direction':'ASCENDING'}], 'limit':150}
            from recommendations import public_product
            records = []
            profiles = {p['tipos'] for p in PRODUCT_PARAMETERS.values() if p['ocasiao'] == facet}
            for page in range(10):
                response = await self.transport('https://firestore.googleapis.com/v1/projects/epav-game/databases/(default)/documents:runQuery',
                    headers={'Authorization':'Bearer '+token},payload={'structuredQuery':dict(query)})
                if response.status == 429: raise ServiceError('CATALOG_QUOTA_EXCEEDED',503,3600)
                if response.status != 200: raise ServiceError('CATALOG_UNAVAILABLE')
                documents = [item['document'] for item in response.json() if 'document' in item]
                for doc in documents:
                    doc_id = doc['name'].rsplit('/',1)[-1]
                    data = {k:decode(v) for k,v in doc.get('fields',{}).items()}
                    card = public_product(doc_id, data)
                    if data.get('disponivelNoJogo') is not True or not card['imagem_url'] or facet not in card['ocasioes']:
                        continue
                    # Redis receives only public fields and usable image URLs.
                    records.append([doc_id, dict(nome=card['nome'],codigo=card['codigo'],
                        disponivelNoJogo=True,tiposProduto=card['tiposProduto'],ocasioes=card['ocasioes'],
                        imagemSwift={'url':card['imagem_url']},dadosOriginais={
                            'Marca':card['marca'],'Formato':card['formato'],'Unidade Medida':card['unidade_medida']})])
                # Continue past a prefix of foods without photos. Stop once every
                # customer sharing this occasion has a varied compatible pool.
                enough = all(len({normalize(data['nome']) for _, data in records
                    if set(data['tiposProduto']) & set(types)}) >= 12 for types in profiles)
                if len(documents) < 150 or enough:
                    break
                query['startAt'] = {'values':[{'referenceValue':documents[-1]['name']}], 'before':False}
            return records
        records = await asyncio.gather(*(self.cache.get_or_load('facet:photos:v2:'+facet,900,
                                      lambda facet=facet: query_facet(facet)) for facet in facets))
        return [record for group in records for record in group]

    async def _menu_catalog(self):
        from recommendations import public_product
        fields = ['nome','codigo','disponivelNoJogo','tiposProduto','ocasioes','imagemSwift.url',
                  'dadosOriginais.Marca','dadosOriginais.Formato','dadosOriginais.Unidade Medida']
        async def load_catalog():
            records, cursor = [], None
            # A single shared lock/cache entry avoids Redis round trips for every page.
            # Twelve pages cover the imported catalog while bounding Worker subrequests.
            for _ in range(12):
                query = {'from':[{'collectionId':'produtos_swift'}],
                    'select':{'fields':[{'fieldPath':'.'.join('`'+p+'`' for p in field.split('.'))} for field in fields]},
                    'where':{'fieldFilter':{'field':{'fieldPath':'disponivelNoJogo'},'op':'EQUAL','value':{'booleanValue':True}}},
                    'orderBy':[{'field':{'fieldPath':'__name__'},'direction':'ASCENDING'}], 'limit':500}
                if cursor: query['startAt']={'values':[{'referenceValue':cursor}],'before':False}
                response=await self.transport('https://firestore.googleapis.com/v1/projects/epav-game/databases/(default)/documents:runQuery',
                    headers={'Authorization':'Bearer '+await self.access_token()},payload={'structuredQuery':query},
                    max_response_chars=2_000_000)
                if response.status == 429: raise ServiceError('CATALOG_QUOTA_EXCEEDED',503,3600)
                if response.status != 200: raise ServiceError('CATALOG_UNAVAILABLE')
                documents=[item['document'] for item in response.json() if 'document' in item]
                for doc in documents:
                    doc_id=doc['name'].rsplit('/',1)[-1]
                    data={k:decode(v) for k,v in doc.get('fields',{}).items()}
                    card=public_product(doc_id,data)
                    if data.get('disponivelNoJogo') is not True or not card['imagem_url'] or not card['nome']: continue
                    records.append([doc_id,card['nome'],card['codigo'],card['tiposProduto'],card['ocasioes'],
                        card['imagem_url'],card['marca'],card['formato'],card['unidade_medida']])
                if len(documents)<500:
                    # Public text/URLs have many repeated prefixes. Compression keeps the
                    # whole catalog within the existing Redis value-size limit.
                    raw=json.dumps(records,separators=(',',':')).encode()
                    return base64.b64encode(zlib.compress(raw)).decode()
                next_cursor=documents[-1]['name']
                if next_cursor==cursor: raise ServiceError('CATALOG_PAGINATION_FAILED',503)
                cursor=next_cursor
            raise ServiceError('CATALOG_TOO_LARGE',503)
        packed=await self.cache.get_or_load('menu:photos:v3:compact',900,load_catalog)
        records=json.loads(zlib.decompress(base64.b64decode(packed)))
        return [[doc_id,dict(nome=name,codigo=code,disponivelNoJogo=True,tiposProduto=types,ocasioes=occasions,
            imagemSwift={'url':photo},dadosOriginais={'Marca':brand,'Formato':format_,'Unidade Medida':unit})]
            for doc_id,name,code,types,occasions,photo,brand,format_,unit in records]

    async def ranking(self):
        return await self.cache.get_or_load('ranking:top20', 30, self._ranking)

    async def _ranking(self):
        fields = ['nome','pontos','qualidadeQuartos','satisfacao','tempoJogadoMs']
        query = {'from':[{'collectionId':'ranking'}],
                 'select':{'fields':[{'fieldPath':field} for field in fields]},
                 'orderBy':[{'field':{'fieldPath':'pontos'},'direction':'DESCENDING'},
                            {'field':{'fieldPath':'tempoJogadoMs'},'direction':'ASCENDING'}], 'limit':20}
        response = await self.transport('https://firestore.googleapis.com/v1/projects/epav-game/databases/(default)/documents:runQuery',
            headers={'Authorization':'Bearer '+await self.access_token()},payload={'structuredQuery':query})
        if response.status == 429: raise ServiceError('CATALOG_QUOTA_EXCEEDED',503,3600)
        if response.status != 200: raise ServiceError('CATALOG_UNAVAILABLE')
        rows=[]
        for item in response.json():
            if 'document' not in item: continue
            data={k:decode(v) for k,v in item['document'].get('fields',{}).items() if k in fields}
            rows.append({field:data.get(field) for field in fields})
        return rows

    async def authenticate_admin(self,id_token):
        response = await self.transport('https://identitytoolkit.googleapis.com/v1/accounts:lookup?key='+self.web_key,
                                        payload={'idToken':id_token})
        if response.status != 200: raise ServiceError('AUTH_INVALID',401)
        try:
            users=response.json()['users']
            if len(users)!=1 or users[0].get('disabled') or json.loads(users[0].get('customAttributes','{}')).get('admin') is not True:
                raise ServiceError('ADMIN_REQUIRED',403)
        except (ValueError,KeyError,TypeError):
            raise ServiceError('ADMIN_REQUIRED',403) from None

_pools = {}
_firebase = {}

def cached_services(config):
    raw = config.get('GROQ_API_KEYS','')
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    if fingerprint not in _pools:
        _pools.clear()
        _pools[fingerprint] = GroqPool(raw)
    return _pools[fingerprint],cached_firebase(config)

def cached_firebase(config):
    account = config.get('FIREBASE_SERVICE_ACCOUNT_JSON','')
    firebase_id = hashlib.sha256((config.get('FIREBASE_WEB_API_KEY','')+account).encode()).hexdigest()
    if firebase_id not in _firebase:
        _firebase.clear()
        from cache import SharedCache, BindingBackend
        binding=config.get('CACHE_BINDING')
        _firebase[firebase_id] = FirebaseService(config.get('FIREBASE_WEB_API_KEY',''),account,
                                               cache=SharedCache(BindingBackend(binding) if binding is not None else None))
    return _firebase[firebase_id]
