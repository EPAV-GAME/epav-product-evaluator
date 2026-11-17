"""PostgreSQL writes: identity stays in Firebase Authentication."""
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timezone
from fastapi import Request, Security, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from services import cached_firebase, ServiceError, decode
from postgres import encode

TYPES=['Carnes','Aves','Pescados','Acompanhamentos','Sobremesas','Despensa']
OCCASIONS=['Praticidade','Dia a dia','Churrasco','Lanches','Receitas','Família','Ocasiões especiais','Leve']
SYNC_COLLECTIONS={'produtos_swift','sincronizacao_imagens_swift','arquivo_produtos_swift'}
ADMIN_COLLECTIONS={'produtos_swift','auditoria_catalogo','ranking'}

class RankingWrite(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    nome:str=Field(min_length=2,max_length=20)
    pontos:StrictInt=Field(ge=0,le=500)
    qualidadeQuartos:StrictInt=Field(ge=0,le=160)
    satisfacao:StrictInt=Field(ge=0,le=100)
    classificacao:str=Field(max_length=40)
    tempoJogadoMs:StrictInt=Field(ge=0,le=604800000)

def now(): return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
def typed(data): return {key:encode(value) for key,value in data.items()}
def plain(row): return dict({k:decode(v) for k,v in row['fields'].items()},id=row['id'],_version=row['revision'])
def identifier(value):
    if not isinstance(value,str) or not 1<=len(value)<=1500 or '/' in value or value in ('.','..'):
        raise ServiceError('VALIDATION',422)
    return value

def validate_edit(original,changes):
    if not isinstance(changes,dict) or set(changes)-{'nome','disponivelNoJogo','tiposProduto','ocasioes','dadosOriginais','imagemSwift'}:
        raise ServiceError('VALIDATION',422)
    data=dict(original,**{k:v for k,v in changes.items() if k!='dadosOriginais'})
    name=data.get('nome'); available=data.get('disponivelNoJogo')
    if not isinstance(name,str) or not 1<=len(name.strip())<=240 or type(available) is not bool:
        raise ServiceError('VALIDATION',422)
    data['nome']=name.strip()
    for field,allowed in [('tiposProduto',TYPES),('ocasioes',OCCASIONS)]:
        values=data.get(field)
        if not isinstance(values,list) or any(not isinstance(v,str) for v in values) or len(set(values))!=len(values) or set(values)-set(allowed):
            raise ServiceError('VALIDATION',422)
    source=dict(original.get('dadosOriginais',{}))
    source.update({'Descricao Produto':data['nome'],'Disponível no jogo':'SIM' if available else 'NÃO',
        'Tipo Produto':'; '.join(data['tiposProduto']),'Ocasiões':'; '.join(data['ocasioes'])})
    for category in TYPES+OCCASIONS:
        source[category]='SIM' if category in data['tiposProduto']+data['ocasioes'] else 'NÃO'
    data['dadosOriginais']=source
    if 'imagemSwift' in changes:
        image=changes['imagemSwift']
        required={'bucket','key','url','format','width','height','bytes','sha256','manual','source','transformVersion','updatedAt'}
        if not isinstance(image,dict) or set(image)!=required: raise ServiceError('VALIDATION',422)
        sha=image.get('sha256','')
        if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha): raise ServiceError('VALIDATION',422)
        expected=dict(bucket='epav-swift-images',key='swift/'+sha+'.webp',
            url='https://epav-swift-images.kevinernandes2012.workers.dev/images/swift/'+sha+'.webp',
            format='webp',width=512,height=512,manual=True,source='admin',transformVersion='admin-webp-v1')
        if any(image.get(k)!=v for k,v in expected.items()) or type(image['bytes']) is not int or not 0<image['bytes']<=102400 or not isinstance(image['updatedAt'],str) or len(image['updatedAt'])>40:
            raise ServiceError('VALIDATION',422)
    return data

def install_data_routes(app,config_for,bearer,authorize):
    async def admin(request,credentials):
        if not credentials: raise HTTPException(401,'FIREBASE_TOKEN_REQUIRED')
        config=config_for(request)
        if request.headers.get('origin') and request.headers['origin'] not in config.get('ALLOWED_ORIGINS','').split(','):
            raise HTTPException(403,'ORIGIN_NOT_ALLOWED')
        service=cached_firebase(config)
        user=await service.authenticate_admin(credentials.credentials)
        return service,user

    def sync(request,credentials):
        secret=config_for(request).get('DATA_SYNC_TOKEN','')
        if request.headers.get('origin') or not credentials or len(secret)<32 or not hmac.compare_digest(secret,credentials.credentials):
            raise HTTPException(403,'SYNC_TOKEN_REQUIRED')
        return cached_firebase(config_for(request))

    async def invalidate(service):
        try:
            await service.cache.invalidate()
            return True
        except OSError: return False # committed writes stay valid; bounded TTL is the fallback.

    @app.post('/v1/ranking')
    async def save_ranking(value:RankingWrite,request:Request,credentials=Security(bearer)):
        service=await authorize(request,credentials,config_for(request))
        uid=await service.authenticate(credentials.credentials)
        await service.database().query('''INSERT INTO epav_data.ranking(uid,nome,pontos,qualidade_quartos,satisfacao,classificacao,tempo_jogado_ms)
          VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(uid) DO UPDATE SET nome=excluded.nome,pontos=excluded.pontos,
          qualidade_quartos=excluded.qualidade_quartos,satisfacao=excluded.satisfacao,classificacao=excluded.classificacao,
          tempo_jogado_ms=excluded.tempo_jogado_ms,publicado_em=clock_timestamp() RETURNING to_jsonb(uid)''',
          [uid,value.nome,value.pontos,value.qualidadeQuartos,value.satisfacao,value.classificacao,value.tempoJogadoMs])
        # Ranking expires in 30 seconds; never evict the expensive product catalog for a score.
        return {'publicado':True,'cache_segundos':30}

    @app.get('/v1/admin/dados/{collection}')
    async def admin_records(collection:str,request:Request,after:str='',limit:int=100,credentials=Security(bearer)):
        service,_=await admin(request,credentials)
        if collection not in ADMIN_COLLECTIONS or not 1<=limit<=300: raise ServiceError('VALIDATION',422)
        if collection=='ranking': rows=await service.database().ranking(True,after,limit+1)
        else: rows=[plain(row) for row in await service.database().records(collection,after,limit+1,recent=collection=='auditoria_catalogo')]
        return {'registros':rows[:limit],'proxima_pagina':rows[limit-1]['id'] if len(rows)>limit else None}

    @app.post('/v1/admin/produtos/{id}')
    async def edit_product(id:str,request:Request,credentials=Security(bearer)):
        service,user=await admin(request,credentials); identifier(id)
        body=await request.json()
        if set(body)!={'version','changes'} or not isinstance(body['version'],str): raise ServiceError('VALIDATION',422)
        db=service.database(); rows=await db.records('produtos_swift',id=id,limit=1)
        if not rows: raise ServiceError('PRODUCT_NOT_FOUND',404)
        row=rows[0]
        if row['revision']!=body['version']: raise ServiceError('CONFLICT',409)
        original={k:decode(v) for k,v in row['fields'].items()}
        updated=validate_edit(original,body['changes']); audit_id=uuid.uuid4().hex
        updated.update(atualizadoEm=now(),atualizadoPor=user['localId'],ultimaAlteracaoId=audit_id)
        # Keep untouched typed values (timestamps, int64, etc.) exactly as imported.
        fields=dict(row['fields'],**typed({k:updated[k] for k in ['nome','disponivelNoJogo','tiposProduto','ocasioes','dadosOriginais','atualizadoEm','atualizadoPor','ultimaAlteracaoId']}))
        source_fields=dict(row['fields'].get('dadosOriginais',{'mapValue':{}})['mapValue'].get('fields',{}))
        for key in ['Descricao Produto','Disponível no jogo','Tipo Produto','Ocasiões']+TYPES+OCCASIONS:
            source_fields[key]=encode(updated['dadosOriginais'][key])
        fields['dadosOriginais']={'mapValue':{'fields':source_fields}}
        fields['atualizadoEm']={'timestampValue':updated['atualizadoEm']}
        if 'imagemSwift' in body['changes']: fields['imagemSwift']=encode(updated['imagemSwift'])
        audit=typed(dict(produtoId=id,autorUid=user['localId'],autorEmail=user.get('email',''),criadoEm=now()))
        audit['criadoEm']={'timestampValue':now()}
        audit.update(antes={'mapValue':{'fields':row['fields']}},depois={'mapValue':{'fields':fields}})
        result=await db.execute([db.put_query('produtos_swift',id,fields,row['revision']),
            db.put_query('auditoria_catalogo',audit_id,audit,'')],batch=True)
        refreshed=await invalidate(service)
        return dict(updated,id=id,_version=result[0][0],_cacheUpdated=refreshed)

    @app.get('/v1/sync/dados/{collection}')
    async def sync_records(collection:str,request:Request,after:str='',limit:int=100,credentials=Security(bearer)):
        service=sync(request,credentials)
        if collection not in SYNC_COLLECTIONS or not 1<=limit<=300: raise ServiceError('VALIDATION',422)
        rows=await service.database().records(collection,after,limit+1)
        return {'documents':rows[:limit],'next':rows[limit-1]['id'] if len(rows)>limit else None}

    @app.post('/v1/sync/patch')
    async def sync_patch(request:Request,credentials=Security(bearer)):
        service=sync(request,credentials); body=await request.json()
        collection=body.get('collection'); id=identifier(body.get('id')); fields=body.get('fields')
        if collection not in {'produtos_swift','sincronizacao_imagens_swift'} or not isinstance(fields,dict): raise ServiceError('VALIDATION',422)
        if collection=='sincronizacao_imagens_swift':
            fields={key:value for key,value in fields.items() if key!='_updatedAt'}
            if set(fields)-{'sourceUrl','checkedAt','status','etag','lastModified','pixelHash'}:
                raise ServiceError('VALIDATION',422)
        if collection=='produtos_swift' and (set(fields)!={'imagemSwift'} or not isinstance(body.get('version'),str)):
            raise ServiceError('VALIDATION',422)
        db=service.database(); rows=await db.records(collection,id=id,limit=1)
        current=rows[0] if rows else None
        if collection=='produtos_swift' and not current: raise ServiceError('PRODUCT_NOT_FOUND',404)
        # A manual admin photo always wins, including during a concurrent sync.
        if collection=='produtos_swift' and decode(current['fields'].get('imagemSwift',{'mapValue':{}})).get('manual') is True:
            raise ServiceError('CONFLICT',409)
        merged=dict(current['fields'] if current else {},**fields)
        expected=body.get('version') if collection=='produtos_swift' else current['revision'] if current else ''
        revision=await db.put(collection,id,merged,expected)
        if collection=='produtos_swift': await invalidate(service)
        return {'revision':revision}

    @app.post('/v1/sync/archive')
    async def archive(request:Request,credentials=Security(bearer)):
        service=sync(request,credentials); body=await request.json(); id=identifier(body.get('id'))
        version=body.get('version'); reason=body.get('reason'); canonical=body.get('canonical_id')
        if not isinstance(version,str) or reason not in ('duplicate','not_found_in_swift') or (reason=='duplicate' and not canonical): raise ServiceError('VALIDATION',422)
        db=service.database(); rows=await db.records('produtos_swift',id=id,limit=1)
        if not rows: return {'status':'already_absent'}
        row=rows[0]
        if decode(row['fields'].get('imagemSwift',{'mapValue':{}})).get('manual') is True: raise ServiceError('CONFLICT',409)
        aid=hashlib.sha256((id+'|'+version).encode()).hexdigest()
        data=typed(dict(produtoId=id,motivo=reason,versaoOriginal=version,duplicadoDe=canonical,arquivadoEm=now()))
        data['dados']={'mapValue':{'fields':row['fields']}}
        queries=[]
        if canonical:
            identifier(canonical)
            if canonical==id: raise ServiceError('VALIDATION',422)
            queries.append(dict(query="SELECT to_jsonb(epav_data.verify_document_revision('produtos_swift',$1,$2))",params=[canonical,body.get('canonical_version')]))
        queries.extend([db.put_query('arquivo_produtos_swift',aid,data,''),
            dict(query="SELECT to_jsonb(epav_data.remove_product($1,$2))",params=[id,version])])
        await db.execute(queries,batch=True); await invalidate(service)
        return {'status':'archived'}

    @app.post('/v1/sync/restore')
    async def restore(request:Request,credentials=Security(bearer)):
        service=sync(request,credentials); body=await request.json(); id=identifier(body.get('id')); db=service.database()
        rows=await db.records('arquivo_produtos_swift',id=id,limit=1)
        if not rows: raise ServiceError('ARCHIVE_NOT_FOUND',404)
        fields=rows[0]['fields']; original_id=identifier(decode(fields['produtoId']))
        await db.put('produtos_swift',original_id,fields['dados']['mapValue']['fields'],'')
        await invalidate(service)
        return {'restored':True}
