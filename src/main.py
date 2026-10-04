import asyncio
import os
import json
import traceback
from fastapi import FastAPI,HTTPException,Request,Security
from fastapi.security import HTTPBearer,HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from models import EvaluationRequest,EvaluationResponse,RecommendationRequest,RecommendationResponse
from context import build_context,ContextError
from evaluator import final_result,provider_payload,RUBRIC_VERSION
from services import cached_services,ServiceError
from recommendations import recommendation_context,select_products

CONFIG_NAMES = ['GROQ_API_KEYS','GROQ_MODEL','FIREBASE_SERVICE_ACCOUNT_JSON','FIREBASE_WEB_API_KEY','ALLOWED_ORIGINS','CACHE_INVALIDATION_TOKEN']

def config_for(request):
    env = request.scope.get('env')
    if env is None:
        return {name:os.environ.get(name,'') for name in CONFIG_NAMES}
    config={name:str(getattr(env,name,'')) for name in CONFIG_NAMES}
    config['CACHE_BINDING']=getattr(env,'REDIS_CACHE',None)
    return config

class BodyLimit:
    def __init__(self,app): self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('method')!='POST':
            return await self.app(scope,receive,send)
        messages,size=[],0
        while True:
            message=await receive()
            if message['type']=='http.disconnect': return
            size+=len(message.get('body',b''))
            if size>32768:
                return await JSONResponse({'detail':'REQUEST_TOO_LARGE'},status_code=413)(scope,receive,send)
            messages.append(message)
            if not message.get('more_body'): break
        async def replay():
            return messages.pop(0) if messages else await receive()
        await self.app(scope,replay,send)

app=FastAPI(title='EPAV — Avaliação de produtos',version='1.1.0',
            description='Avaliação pedagógica de adequação: 0 a 1000, usando cenários oficiais e catálogo Firebase.')
app.add_middleware(BodyLimit)
app.add_middleware(CORSMiddleware,allow_origins=['https://epav-game.github.io'],
                   allow_methods=['GET','POST'],allow_headers=['Authorization','Content-Type'])
bearer=HTTPBearer(auto_error=False,description='ID token Firebase do projeto epav-game.')

async def authorize(request,credentials,config):
    if request.headers.get('origin') and request.headers['origin'] not in config.get('ALLOWED_ORIGINS','').split(','):
        raise HTTPException(403,'ORIGIN_NOT_ALLOWED')
    if credentials is None or not 20 <= len(credentials.credentials) <= 4096:
        raise HTTPException(401,'FIREBASE_TOKEN_REQUIRED')
    from services import cached_firebase
    firebase = cached_firebase(config)
    uid = await firebase.authenticate(credentials.credentials)
    env = request.scope.get('env')
    if env is not None:
        for name,key in [('IP_LIMIT',request.headers.get('cf-connecting-ip','unknown')),('USER_LIMIT',uid),('GLOBAL_LIMIT','evaluations')]:
            result=await getattr(env,name).limit({'key':key})
            if not result.success: raise ServiceError('RATE_LIMITED',429,60)
    return firebase

@app.post('/v1/recomendacoes',response_model=RecommendationResponse)
async def recommendations(choice:RecommendationRequest,request:Request,credentials:HTTPAuthorizationCredentials|None=Security(bearer)):
    try:
        async with asyncio.timeout(40):
            firebase = await authorize(request,credentials,config_for(request))
            context = recommendation_context(choice)
            return select_products(context,await firebase.catalog(context))
    except ContextError as error:
        raise HTTPException(422,str(error)) from None
    except TimeoutError:
        raise ServiceError('CATALOG_TIMEOUT',503,30) from None

@app.exception_handler(ServiceError)
async def service_error(request,error):
    headers={'Retry-After':str(error.retry_after)} if error.retry_after else {}
    return JSONResponse({'detail':error.code},status_code=error.status,headers=headers)

@app.get('/health')
async def health(request:Request):
    config=config_for(request)
    return dict(service='epav-product-evaluator',configured=bool(config['GROQ_API_KEYS'] and config['FIREBASE_SERVICE_ACCOUNT_JSON']),versao_rubrica=RUBRIC_VERSION, cache_configured=config.get('CACHE_BINDING') is not None)

@app.get('/v1/ranking')
async def ranking(request:Request):
    from services import cached_firebase
    async with asyncio.timeout(40):
        return {'resultados':await cached_firebase(config_for(request)).ranking(), 'cache_segundos':30}

async def authorize_cache(request,credentials):
    import hmac
    from services import cached_firebase
    config=config_for(request)
    if request.headers.get('origin') and request.headers['origin'] not in config.get('ALLOWED_ORIGINS','').split(','):
        raise HTTPException(403,'ORIGIN_NOT_ALLOWED')
    if credentials is None or not 20 <= len(credentials.credentials) <= 4096:
        raise HTTPException(401,'FIREBASE_TOKEN_REQUIRED')
    firebase=cached_firebase(config)
    secret=config.get('CACHE_INVALIDATION_TOKEN','')
    if not (len(secret)>=32 and hmac.compare_digest(credentials.credentials,secret)):
        await firebase.authenticate_admin(credentials.credentials)
    return firebase

@app.post('/v1/cache/invalidate')
async def invalidate_cache(request:Request,credentials:HTTPAuthorizationCredentials|None=Security(bearer)):
    firebase=await authorize_cache(request,credentials)
    try: await firebase.cache.invalidate()
    except OSError: raise ServiceError('CACHE_UNAVAILABLE',503,30) from None
    return {'invalidado':True,'propagacao_maxima_segundos':5}

@app.get('/v1/cache/status')
async def cache_status(request:Request,credentials:HTTPAuthorizationCredentials|None=Security(bearer)):
    import uuid
    firebase=await authorize_cache(request,credentials)
    key='probe:'+uuid.uuid4().hex
    value=uuid.uuid4().hex
    try:
        reachable=bool(await firebase.cache.invoke('ping'))
        await firebase.cache.invoke('claim',key=key,value=value,ttl=10)
        entry=await firebase.cache.invoke('read',key=key)
        reachable=reachable and entry['value']==value and entry['ttl_ms']>0
        await firebase.cache.invoke('release',key=key,value=value)
    except OSError: reachable=False
    return {'redis_disponivel':reachable,'catalogo_ttl':900,'produto_ttl':60,'ranking_ttl':30}

@app.post('/v1/avaliacoes',response_model=EvaluationResponse)
async def evaluate(choice:EvaluationRequest,request:Request,credentials:HTTPAuthorizationCredentials|None=Security(bearer)):
    config=config_for(request)
    origins=config.get('ALLOWED_ORIGINS','').split(',')
    if request.headers.get('origin') and request.headers['origin'] not in origins:
        raise HTTPException(403,'ORIGIN_NOT_ALLOWED')
    if credentials is None or not 20<=len(credentials.credentials)<=4096:
        raise HTTPException(401,'FIREBASE_TOKEN_REQUIRED')
    env=request.scope.get('env')
    stage='authenticate'
    try:
        async with asyncio.timeout(40):
            # Authenticate before parsing Groq keys; unconfigured service never becomes public.
            from services import FirebaseService
            firebase = FirebaseService(config['FIREBASE_WEB_API_KEY'],config['FIREBASE_SERVICE_ACCOUNT_JSON'])
            uid=await firebase.authenticate(credentials.credentials)
            stage='rate_limits'
            if env is not None:
                for name,key in [('IP_LIMIT',request.headers.get('cf-connecting-ip','unknown')),('USER_LIMIT',uid),('GLOBAL_LIMIT','evaluations')]:
                    result=await getattr(env,name).limit({'key':key})
                    if not result.success:
                        raise ServiceError('RATE_LIMITED',429,60)
            pool,firebase=cached_services(config)
            stage='catalog'
            product=await firebase.product(choice.produto_id)
            context=build_context(choice,product)
            model=config.get('GROQ_MODEL') or 'openai/gpt-oss-20b'
            stage='groq'
            judgement=await pool.complete(provider_payload(context,model))
            return final_result(judgement,context,model)
    except ContextError as error:
        raise HTTPException(422,str(error)) from None
    except ServiceError:
        raise
    except TimeoutError:
        raise ServiceError('EVALUATION_TIMEOUT',503,30) from None
    except Exception as error:
        # Keep provider response bodies, JWTs and credentials out of logs and HTTP responses.
        markers = [word for word in ['TypeError','PyProxy','BufferSource','headers','body','Abort','fetch',
                   'snapshot','outside','undefined','Promise','keyword','illegal invocation','String','JSON','permission',
                   'not a function','not callable','null','object','array','constructor','argument','unsupported','iterable']
                   if word.lower() in str(error).lower()]
        frames=[{'file':os.path.basename(frame.filename),'line':frame.lineno,'function':frame.name}
                for frame in traceback.extract_tb(error.__traceback__)[-4:]]
        print(json.dumps({'event':'evaluation_failed','stage':stage,'reason':type(error).__name__,'markers':markers,'frames':frames}))
        raise ServiceError('EVALUATION_FAILED',502) from None

try:
    from workers import asgi
    Default=asgi.entrypoint(app)
except ImportError:
    # Native development: uv run uvicorn main:app --app-dir src.
    pass
