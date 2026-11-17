"""Parameterized Neon HTTP transport for the Python Worker.

Protocol follows neondatabase/serverless src/http/index.ts. Never logs SQL,
credentials or upstream error messages. JSON is transport, not a stored column.
"""
import json
import urllib.parse
from services import ServiceError, post_or_get

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

def flatten(fields):
    rows=[]
    def visit(wrapped,parent,name,index):
        if not isinstance(wrapped,dict) or len(wrapped)!=1: raise ServiceError('VALIDATION',422)
        kind,value=next(iter(wrapped.items()))
        row=dict(node_no=len(rows)+1,parent_no=parent,field_name=name,array_index=index,kind=kind)
        rows.append(row)
        if kind in ('mapValue','arrayValue'):
            key='fields' if kind=='mapValue' else 'values'
            row['members_present']=key in value
            for k,v in (value.get(key,{}) .items() if key=='fields' else enumerate(value.get(key,[]))):
                visit(v,row['node_no'],k if key=='fields' else None,k if key=='values' else None)
        elif kind=='geoPointValue': row.update(latitude=value['latitude'],longitude=value['longitude'])
        elif kind=='nullValue': pass
        elif kind in SCALARS: row[SCALARS[kind]]=value
        else: raise ServiceError('VALIDATION',422)
    for name,value in fields.items(): visit(value,None,name,None)
    return rows

def rebuild(rows):
    root,parents={},{}
    for row in rows:
        kind=row['kind']
        if kind in ('mapValue','arrayValue'):
            key='fields' if kind=='mapValue' else 'values'
            value={key:{} if key=='fields' else []} if row['members_present'] else {}
        elif kind=='nullValue': value=None
        elif kind=='geoPointValue': value=dict(latitude=row['latitude'],longitude=row['longitude'])
        else: value=row[SCALARS[kind]]
        wrapped={kind:value}
        if row['parent_no'] is None: root[row['field_name']]=wrapped
        else:
            parent=parents[row['parent_no']]
            if 'mapValue' in parent: parent['mapValue']['fields'][row['field_name']]=wrapped
            else: parent['arrayValue']['values'].append(wrapped)
        parents[row['node_no']]=wrapped
    return root

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
        return dict(query='SELECT to_jsonb(epav_data.put_record($1,$2,$3,$4::jsonb))',
            params=[collection,id,expected,json.dumps(flatten(fields),separators=(',',':'))])

    async def put(self,collection,id,fields,expected=None):
        return (await self.execute([self.put_query(collection,id,fields,expected)]))[0]

    async def records(self,collection,after='',limit=100,id=None,recent=False):
        # Return only the requested page, preserving original typed values exactly.
        result=await self.query('''SELECT jsonb_build_object('id',r.document_id,'revision',r.revision,
          'fields',coalesce((SELECT jsonb_agg((to_jsonb(f)-'document_row_id'-'timestamp_value'-'bytes_value'-'integer_value')
            ||jsonb_build_object('timestamp_value',to_char(f.timestamp_value AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
              'bytes_value',replace(encode(f.bytes_value,'base64'),chr(10),''),'integer_value',f.integer_value::varchar)
            ORDER BY f.node_no) FROM epav_data.record_fields f WHERE f.document_row_id=r.row_id),'[]'::jsonb))
          FROM epav_data.records r WHERE r.collection_path=$1
            AND (CASE WHEN $5::boolean THEN ($2='' OR (r.created_at,r.document_id)<
              (SELECT created_at,document_id FROM epav_data.records WHERE collection_path=$1 AND document_id=$2))
              ELSE r.document_id>$2 END)
            AND ($4::varchar IS NULL OR r.document_id=$4)
          ORDER BY CASE WHEN $5::boolean THEN r.created_at END DESC,
            CASE WHEN $5::boolean THEN r.document_id END DESC,r.document_id LIMIT $3''',
            [collection,after,limit,id,recent])
        for row in result: row['fields']=rebuild(row['fields'])
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
