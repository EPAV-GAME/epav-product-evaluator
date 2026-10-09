import json
import random
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from context import build_context, ContextError
from models import RecommendationRequest, EvaluationRequest
from recommendations import recommendation_context, select_products
from menu import CATEGORIES, MENU_STAGES, roles, selection_context, previous_context
from scenarios import SCENARIOS
from evaluator import assessment_facts, provider_payload
from services import FirebaseService, HTTPResult
from main import app
from cache import SharedCache
from test_cache import MemoryRedis

PHOTO='https://epav-swift-images.kevinernandes2012.workers.dev/images/swift/'+'a'*64+'.webp'
def choice(category,client='cliente1'):
    stage=MENU_STAGES[client][category]; node=SCENARIOS[client]['noInicial']; history=[]
    while node!=stage:
        option=SCENARIOS[client]['dialogo'][node]['opcoes'][0]
        history.append(dict(no_id=node,opcao_id=option['id']));node=option['proximoNo']
    return dict(cliente_id=client,no_atual=stage,categoria=category,historico=history)

def catalog():
    groups=[('Pão de alho','Acompanhamentos'),('Carne','Carnes'),('Arroz','Acompanhamentos'),('Suco','Despensa'),('Sorvete','Sobremesas')]
    return [(str(group)+'-'+str(i),dict(nome=name+' '+str(i),codigo=str(group)+'-'+str(i),
        tiposProduto=[kind],ocasioes=['Churrasco'],disponivelNoJogo=True,imagemSwift={'url':PHOTO}))
        for group,(name,kind) in enumerate(groups) for i in range(14)]

class MenuTest(unittest.TestCase):
    def test_ingredients_in_a_meat_name_do_not_turn_it_into_an_appetizer(self):
        self.assertEqual(roles({'nome':'Linguiça com queijo coalho','tiposProduto':['Carnes']}),{'principal'})
        self.assertEqual(roles({'nome':'Coxinha da asa temperada','tiposProduto':['Aves']}),{'principal'})
        self.assertIn('entrada',roles({'nome':'Queijo coalho peça','tiposProduto':['Acompanhamentos']}))
    def test_all_clients_get_ten_distinct_photo_products_in_each_revealed_stage(self):
        for client in SCENARIOS:
            for category in CATEGORIES:
                context=recommendation_context(RecommendationRequest(**choice(category,client)))
                answer=select_products(context,catalog(),rng=random.Random(1))
                self.assertEqual(len(answer['produtos']),10)
                self.assertEqual(len({p['codigo'] for p in answer['produtos']}),10)
                self.assertTrue(all(category in roles(p) and p['imagem_url'] for p in answer['produtos']))
                self.assertEqual(context['conversa'][-1]['id'],MENU_STAGES[client][category])
                self.assertEqual(answer['categoria'],category)

    def test_short_or_empty_category_reports_real_inventory_never_repeats_or_substitutes(self):
        context=recommendation_context(RecommendationRequest(**choice('bebida')))
        records=[entry for entry in catalog() if entry[0] in ['3-0','3-1'] or not entry[0].startswith('3-')]
        output=select_products(context,records)
        self.assertEqual(len(output['produtos']),2);self.assertEqual(output['total_disponiveis'],2)
        empty=select_products(context,[])
        self.assertEqual(empty['produtos'],[]);self.assertEqual(empty['total_disponiveis'],0)
        self.assertEqual(empty['quantidade_solicitada'],10)

    def test_wrong_stage_future_or_repeated_choices_are_rejected(self):
        bad=dict(choice('entrada'),no_atual='d1')
        with self.assertRaises(ContextError): recommendation_context(RecommendationRequest(**bad))
        for categories in [['sobremesa'],['entrada','entrada'],['principal','entrada']]:
            request=EvaluationRequest(**choice('acompanhamento'),produto_id='now',escolhas_anteriores=[
                dict(categoria=c,produto_id='old',quantidade=dict(unidades=1)) for c in categories])
            with self.assertRaises(ContextError):selection_context(request,build_context(request,{}))

    def test_dessert_and_drinks_are_evaluated_as_their_role_instead_of_as_meat(self):
        for category,name,kind in [('sobremesa','Sorvete','Sobremesas'),('bebida','Suco','Despensa')]:
            request=EvaluationRequest(**choice(category),produto_id='now',quantidade=dict(unidades=1))
            context=selection_context(request,build_context(request,dict(nome=name,tiposProduto=[kind],ocasioes=['Lanches'])))
            checks=assessment_facts(context)
            self.assertFalse(checks['tipo_incompativel']);self.assertFalse(checks['ocasiao_incompativel'])
            context['produto']['nome']='Arroz';context['produto']['tiposProduto']=['Acompanhamentos']
            self.assertTrue(assessment_facts(context)['tipo_incompativel'])

    def test_category_endpoint_never_calls_groq_and_still_requires_auth(self):
        with TestClient(app) as client:
            payload=choice('entrada')
            self.assertEqual(client.post('/v1/recomendacoes',json=payload).status_code,401)
            with patch('main.authorize',AsyncMock(return_value=type('DB',(),{'catalog':AsyncMock(return_value=catalog())})())), \
                 patch('main.cached_services',side_effect=AssertionError('No Groq for product selection')):
                response=client.post('/v1/recomendacoes',json=payload,headers={'Authorization':'Bearer '+'t'*30})
                self.assertEqual(response.status_code,200)
                self.assertEqual(len(response.json()['produtos']),10)

class MenuContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_previous_products_are_loaded_from_trusted_catalog_not_player_labels(self):
        request=EvaluationRequest(**choice('principal'),produto_id='now',quantidade=dict(unidades=1),
            escolhas_anteriores=[dict(categoria='entrada',produto_id='before',quantidade=dict(unidades=2))])
        firebase=type('DB',(),{'product':AsyncMock(return_value={'id':'before','nome':'Pão de alho'})})()
        context=selection_context(request,build_context(request,{'id':'now','nome':'Carne'}))
        context['produtos_anteriores']=await previous_context(request,firebase)
        payload=provider_payload(context,'openai/gpt-oss-20b')
        data=json.loads(payload['messages'][1]['content'])
        self.assertEqual(data['produtos_anteriores'][0]['produto']['nome'],'Pão de alho')
        self.assertEqual(data['produtos_anteriores'][0]['quantidade']['unidades'],2)
        self.assertIn('cardapio',payload['response_format']['json_schema']['schema']['$defs']['Criterion']['properties']['evidencias']['items']['enum'])
        firebase.product.assert_awaited_once_with('before')

    async def test_menu_pages_are_shared_across_categories_and_never_cache_private_fields(self):
        def encode(value):
            if isinstance(value,dict):return {'mapValue':{'fields':{k:encode(v) for k,v in value.items()}}}
            if isinstance(value,list):return {'arrayValue':{'values':[encode(v) for v in value]}}
            if isinstance(value,bool):return {'booleanValue':value}
            return {'stringValue':value}
        documents=[{'document':{'name':'projects/epav-game/databases/(default)/documents/produtos_swift/'+str(i),
            'fields':{k:encode(v) for k,v in dict(catalog()[i%len(catalog())][1],codigo=str(i),nome='Carne '+str(i),
                margem='private',fornecedor='private',dadosOriginais={'Marca':'Swift','Margem':'private'}).items()}}} for i in range(501)]
        transport=AsyncMock(side_effect=[HTTPResult(200,{},json.dumps(documents[:500])),HTTPResult(200,{},json.dumps(documents[500:]))])
        backend=MemoryRedis()
        db=FirebaseService('public','{}',transport,cache=SharedCache(backend))
        with patch.object(db,'access_token',AsyncMock(return_value='token')):
            first=await db.catalog({'categoria_refeicao':'entrada'})
            second=await db.catalog({'categoria_refeicao':'principal'})
        another=FirebaseService('public','{}',transport,cache=SharedCache(backend))
        self.assertEqual(await another.catalog({'categoria_refeicao':'sobremesa'}),first)
        self.assertEqual(len(first),501);self.assertEqual(first,second)
        self.assertEqual(transport.call_count,2)
        self.assertEqual(transport.call_args_list[0].kwargs['payload']['structuredQuery']['limit'],500)
        self.assertEqual(transport.call_args_list[1].kwargs['payload']['structuredQuery']['startAt']['values'][0]['referenceValue'],documents[499]['document']['name'])
        self.assertEqual(len(db.cache.memory),1)
        self.assertEqual(len(backend.data),1)
        self.assertNotIn('private',json.dumps(first))
        self.assertTrue(all(len(json.dumps(value))<256000 for _,value in db.cache.memory.values()))
        fiche=await another.menu_product('0')
        self.assertEqual(fiche['id'],'0')
        self.assertEqual(transport.call_count,2)
        self.assertNotIn('imagemSwift',fiche)
        self.assertNotIn('private',json.dumps(fiche))
        from services import ServiceError
        with self.assertRaises(ServiceError):await another.menu_product('missing')
        self.assertEqual(transport.call_count,2)

    async def test_verified_context_endpoint_requires_auth_and_never_invokes_groq(self):
        data=choice('principal');data.update(produto_id='p',quantidade={'unidades':2})
        db=type('DB',(),{'menu_product':AsyncMock(return_value={'id':'p','nome':'Carne','tiposProduto':['Carnes']}),
            'product':AsyncMock(side_effect=AssertionError('Do not refetch menu products'))})()
        with TestClient(app) as client:
            self.assertEqual(client.post('/v1/contexto',json=data).status_code,401)
            with patch('main.authorize',AsyncMock(return_value=db)),patch('main.cached_services',side_effect=AssertionError('No AI')):
                result=client.post('/v1/contexto',json=data,headers={'Authorization':'Bearer '+'t'*30})
                self.assertEqual(result.status_code,200,result.text)
                self.assertEqual(result.json()['produto']['nome'],'Carne')
                bad=client.post('/v1/contexto',json=dict(data,no_atual='d1'),headers={'Authorization':'Bearer '+'t'*30})
                self.assertEqual(bad.status_code,422)
                db.menu_product.assert_awaited_once_with('p')
