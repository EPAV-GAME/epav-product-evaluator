import asyncio
import base64
import hashlib
import json
import re
import sys
import time
import urllib.parse
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

async def post_or_get(url, *, method='POST', headers=None, payload=None, form=None):
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
    if len(result.body) > 256_000:
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
    def __init__(self,web_key,account_raw,transport=post_or_get):
        self.web_key,self.transport = web_key,transport
        self.account_raw = account_raw
        self.token,self.expires = '',0
        self.catalog_cache = {}
        self.catalog_lock = asyncio.Lock()

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
        from recommendations import normalize
        text = normalize((context or {}).get('cliente',{}).get('perfil',''))
        occasion = 'Churrasco' if 'churrasco' in text else 'Praticidade' if any(w in text for w in ['pressa','pratic']) else 'Dia a dia'
        facets = ['Carnes','Aves','Pescados',occasion]
        async with self.catalog_lock:
            missing = [facet for facet in facets if self.catalog_cache.get(facet,(0,[]))[0] <= time.time()]
            if not missing: return [record for facet in facets for record in self.catalog_cache[facet][1]]
            token = await self.access_token()
            fields = ['nome','codigo','disponivelNoJogo','tiposProduto','ocasioes','imagemSwift.url',
                      'dadosOriginais.Marca','dadosOriginais.Formato','dadosOriginais.Unidade Medida']
            async def query_facet(facet):
                # The admin panel synchronizes SIM/NÃO flags with types and occasions.
                # Equality-index merging avoids a full catalog scan or new composite indexes.
                query = {'from':[{'collectionId':'produtos_swift'}],
                     'select':{'fields':[{'fieldPath':'.'.join('`'+part+'`' for part in field.split('.'))} for field in fields]},
                     'where':{'compositeFilter':{'op':'AND','filters':[
                         {'fieldFilter':{'field':{'fieldPath':'disponivelNoJogo'},'op':'EQUAL','value':{'booleanValue':True}}},
                         {'fieldFilter':{'field':{'fieldPath':'`dadosOriginais`.`'+facet+'`'},'op':'EQUAL','value':{'stringValue':'SIM'}}}]}},
                     'orderBy':[{'field':{'fieldPath':'__name__'},'direction':'ASCENDING'}], 'limit':150}
                response = await self.transport('https://firestore.googleapis.com/v1/projects/epav-game/databases/(default)/documents:runQuery',
                    headers={'Authorization':'Bearer '+token},payload={'structuredQuery':query})
                if response.status == 429: raise ServiceError('CATALOG_QUOTA_EXCEEDED',503,3600)
                if response.status != 200: raise ServiceError('CATALOG_UNAVAILABLE')
                docs = [item['document'] for item in response.json() if 'document' in item]
                records = [(doc['name'].rsplit('/',1)[-1], {k:decode(v) for k,v in doc.get('fields',{}).items()}) for doc in docs]
                return facet, records
            for facet, records in await asyncio.gather(*(query_facet(facet) for facet in missing)):
                self.catalog_cache[facet] = time.time()+3600, records
            return [record for facet in facets for record in self.catalog_cache[facet][1]]

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
        _firebase[firebase_id] = FirebaseService(config.get('FIREBASE_WEB_API_KEY',''),account)
    return _firebase[firebase_id]
