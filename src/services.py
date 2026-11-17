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
        repaired = False
        for index, key in enumerate(self.keys):
            if self.cooldown.get(index,0)>self.clock():
                continue
            try:
                response = await asyncio.wait_for(self.transport(
                    'https://api.groq.com/openai/v1/chat/completions',
                    headers={'Authorization':'Bearer '+key},payload=payload),timeout=12)
                # Some valid strict-schema requests fail during generation. Retry
                # once on the same key in JSON mode, preserving the schema in the
                # instructions. Pydantic and the evidence checks still gate scores.
                if response.status == 400 and not repaired:
                    try: code = response.json().get('error',{}).get('code')
                    except (ValueError,TypeError,AttributeError): code = None
                    if code == 'json_validate_failed' and payload.get('response_format',{}).get('type') == 'json_schema':
                        repaired = True
                        repair = dict(payload, response_format={'type':'json_object'}, max_completion_tokens=3200,
                            messages=[*payload['messages'], {'role':'system','content':
                                'Retorne somente um objeto JSON que corresponda exatamente a este schema. '
                                'Respeite os limites e referências; não inclua campos adicionais: '+
                                json.dumps(payload['response_format']['json_schema']['schema'],ensure_ascii=False)}])
                        response = await asyncio.wait_for(self.transport(
                            'https://api.groq.com/openai/v1/chat/completions',
                            headers={'Authorization':'Bearer '+key},payload=repair),timeout=12)
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
                    provider_error=response.json().get('error',{})
                    code=provider_error.get('code')
                    # A provider's schema failure can still contain a complete
                    # candidate. Accept it only under our unchanged strict model;
                    # final_result also validates context references and score caps.
                    if code == 'json_validate_failed' and isinstance(provider_error.get('failed_generation'),str):
                        try: return AIJudgement.model_validate_json(provider_error['failed_generation'])
                        except ValidationError as validation:
                            print(json.dumps({'event':'groq_generation_invalid','types':sorted({item['type'] for item in validation.errors()})}))
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

def decode(value):
    if 'mapValue' in value:
        return {k:decode(v) for k,v in value['mapValue'].get('fields',{}).items()}
    if 'arrayValue' in value:
        return [decode(v) for v in value['arrayValue'].get('values',[])]
    if 'integerValue' in value:
        return int(value['integerValue'])
    return next(iter(value.values()))

class FirebaseService:
    def __init__(self,web_key,account_raw="",transport=post_or_get,cache=None,database_url=""):
        self.web_key,self.transport = web_key,transport
        self.database_url = database_url
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

    def database(self):
        from postgres import Postgres
        return Postgres(self.database_url,self.transport)

    async def product(self,doc_id):
        return await self.cache.get_or_load('postgres:product:'+doc_id,60,lambda:self._product(doc_id))

    async def _product(self,doc_id):
        rows=await self.database().catalog(doc_id)
        if not rows: raise ServiceError('PRODUCT_NOT_AVAILABLE',422)
        data=rows[0]
        from recommendations import public_product
        card=public_product(doc_id,data)
        return {key:card[key] for key in ('id','nome','tiposProduto','ocasioes',
            'marca','formato','unidade_medida','peso_embalagem_kg')}

    async def menu_product(self,doc_id):
        for identifier,data in await self._menu_catalog():
            if identifier==doc_id:
                from recommendations import public_product
                card=public_product(identifier,data)
                return {key:card[key] for key in ('id','nome','tiposProduto','ocasioes',
                    'marca','formato','unidade_medida','peso_embalagem_kg')}
        raise ServiceError('PRODUCT_NOT_AVAILABLE',422)

    async def catalog(self,context=None):
        records=await self._menu_catalog()
        if context and context.get('categoria_refeicao'): return records
        from recommendations import product_parameters
        occasion=product_parameters(context)['ocasiao'] if context else 'Dia a dia'
        return [[id,data] for id,data in records if occasion in data.get('ocasioes',[])]

    async def _menu_catalog(self):
        from recommendations import public_product
        async def load_catalog():
            records=[]
            for data in await self.database().catalog():
                card=public_product(data['id'],data)
                if not card['imagem_url'] or not card['nome']: continue
                records.append([card['id'],card['nome'],card['codigo'],card['tiposProduto'],card['ocasioes'],
                    card['imagem_url'],card['marca'],card['formato'],card['unidade_medida']])
            return base64.b64encode(zlib.compress(json.dumps(records,separators=(',',':')).encode())).decode()
        packed=await self.cache.get_or_load('postgres:menu:photos:v1',900,load_catalog)
        records=json.loads(zlib.decompress(base64.b64decode(packed)))
        return [[id,dict(nome=name,codigo=code,disponivelNoJogo=True,tiposProduto=types,ocasioes=occasions,
            imagemSwift={'url':photo},dadosOriginais={'Marca':brand,'Formato':format_,'Unidade Medida':unit})]
            for id,name,code,types,occasions,photo,brand,format_,unit in records]

    async def ranking(self):
        return await self.cache.get_or_load('postgres:ranking:top20',30,self._ranking)

    async def _ranking(self):
        fields=('nome','pontos','qualidadeQuartos','satisfacao','tempoJogadoMs')
        return [{key:row[key] for key in fields} for row in await self.database().ranking()]

    async def authenticate_admin(self,id_token):
        response = await self.transport('https://identitytoolkit.googleapis.com/v1/accounts:lookup?key='+self.web_key,
                                        payload={'idToken':id_token})
        if response.status != 200: raise ServiceError('AUTH_INVALID',401)
        try:
            users=response.json()['users']
            if len(users)!=1 or users[0].get('disabled') or json.loads(users[0].get('customAttributes','{}')).get('admin') is not True:
                raise ServiceError('ADMIN_REQUIRED',403)
            return users[0]
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
    account = config.get('DATABASE_URL','')
    firebase_id = hashlib.sha256((config.get('FIREBASE_WEB_API_KEY','')+account).encode()).hexdigest()
    if firebase_id not in _firebase:
        _firebase.clear()
        from cache import SharedCache, BindingBackend
        binding=config.get('CACHE_BINDING')
        _firebase[firebase_id] = FirebaseService(config.get('FIREBASE_WEB_API_KEY',''),account,
                                               cache=SharedCache(BindingBackend(binding) if binding is not None else None),database_url=account)
    return _firebase[firebase_id]
