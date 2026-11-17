"""Parameterized Neon HTTP transport for the Python Worker.

Protocol follows neondatabase/serverless src/http/index.ts. Never logs SQL,
credentials or upstream error messages. JSON is transport, not a stored column.
"""
import json
import urllib.parse
from services import ServiceError, post_or_get, decode

SCALARS = dict(stringValue='string_value',integerValue='integer_value',doubleValue='double_value',
    booleanValue='boolean_value',timestampValue='timestamp_value',bytesValue='bytes_value',referenceValue='reference_value')

def encode(value):
    if value is None: return {'nullValue':None}
    if isinstance(value,bool): return {'booleanValue':value}
    if isinstance(value,int): return {'integerValue':str(value)}
    if isinstance(value,float): return {'doubleValue':value}
    if isinstance(value,dict): return {'mapValue':{'fields':{k:encode(v) for k,v in value.items()}}}
    if isinstance(value,list): return {'arrayValue':{'values':[encode(v) for v in value]}}
    return {'stringValue':str(value)}

class Postgres:
    def __init__(self,url,transport=post_or_get):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme not in ('postgres','postgresql') or not (parsed.hostname or '').endswith('.neon.tech'):
            raise ServiceError('DATABASE_NOT_CONFIGURED')
        self.url,self.endpoint,self.transport=url,'https://'+parsed.hostname+'/sql',transport

    async def execute(self,queries,batch=False):
        headers={'Neon-Connection-String':self.url,'Neon-Raw-Text-Output':'true','Neon-Array-Mode':'true'}
        if batch: headers['Neon-Batch-Isolation-Level']='Serializable'
        response=await self.transport(self.endpoint,headers=headers,
            payload={'queries':queries} if batch else queries[0],max_response_chars=12_000_000)
        if response.status!=200:
            try: code=response.json().get('code')
            except (ValueError,AttributeError): code=None
            if code in ('40001','23505'): raise ServiceError('CONFLICT',409)
            if code in ('23514','22001','22P02'): raise ServiceError('VALIDATION',422)
            raise ServiceError('DATABASE_UNAVAILABLE',503,10)
        data=response.json()
        def unpack(result): return [json.loads(row[0]) if row[0] is not None else None for row in result['rows']]
        return [unpack(result) for result in data['results']] if batch else unpack(data)

    async def query(self,sql,params=None):
        return await self.execute([dict(query=sql,params=params or [])])

    def put_query(self,collection,id,fields,expected=None):
        return dict(query='SELECT to_jsonb(epav_data.save_document($1,$2,$3,$4::jsonb))',
            params=[collection,id,expected,json.dumps({key:decode(value) for key,value in fields.items()},separators=(',',':'))])

    async def put(self,collection,id,fields,expected=None):
        return (await self.execute([self.put_query(collection,id,fields,expected)]))[0]

    async def records(self,collection,after='',limit=100,id=None,recent=False):
        result=await self.query('SELECT * FROM epav_data.list_documents($1,$2,$3,$4,$5)',
            [collection,after,limit,id,recent])
        for row in result:
            row['fields']={key:encode(value) for key,value in row.pop('data').items()}
        return result

    async def catalog(self,id=None):
        return await self.query('''SELECT jsonb_build_object('id',id,'nome',nome,'codigo',codigo,
          'tiposProduto',tipos,'ocasioes',ocasioes,'disponivelNoJogo',disponivel,
          'imagemSwift',jsonb_build_object('url',imagem_url),
          'dadosOriginais',jsonb_build_object('Marca',marca,'Formato',formato,'Unidade Medida',unidade))
          FROM epav_data.products WHERE disponivel IS TRUE AND ($1::varchar IS NULL OR id=$1)
          ORDER BY id''',[id])

    async def ranking(self,admin=False,after='',limit=20):
        return await self.query('''SELECT jsonb_build_object('id',uid,'nome',nome,'pontos',pontos,
          'qualidadeQuartos',qualidade_quartos,'satisfacao',satisfacao,'tempoJogadoMs',tempo_jogado_ms,
          'classificacao',classificacao,'publicadoEm',publicado_em)
          FROM epav_data.ranking WHERE ($1::boolean IS FALSE OR uid>$2)
          ORDER BY CASE WHEN $1::boolean THEN 0 ELSE pontos END DESC,
            CASE WHEN $1::boolean THEN 0 ELSE tempo_jogado_ms END,uid LIMIT $3''',[admin,after,limit])
