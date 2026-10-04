import unittest
import random
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from models import RecommendationRequest
from context import ContextError
from recommendations import recommendation_context, select_products, package_weight, public_product, PRODUCT_PARAMETERS, STAGES
from scenarios import SCENARIOS
from services import FirebaseService, HTTPResult, ServiceError
from main import app
import json

def context():
    return recommendation_context(RecommendationRequest(cliente_id='cliente1',no_atual='d3',historico=[
        {'no_id':'d1','opcao_id':'d1-o1'},{'no_id':'d2','opcao_id':'d2-o1'}]))

def products():
    data = dict(disponivelNoJogo=True,tiposProduto=['Carnes'],ocasioes=['Churrasco'],
                dadosOriginais={'Marca':'Swift','Margem':99,'Nome Fornecedor':'private'},
                imagemSwift={'url':'https://epav-swift-images.kevinernandes2012.workers.dev/images/swift/'+'a'*64+'.webp'})
    return [(str(i),dict(data,nome='Produto '+str(i),codigo=str(i))) for i in range(4)]

class RecommendationTest(unittest.TestCase):
    def test_missing_empty_and_invalid_photos_are_excluded_even_if_other_fields_match(self):
        no_photos = [(f'missing-{i}',dict(products()[0][1],nome=f'Missing {i}',codigo=f'missing-{i}',
            imagemSwift=photo)) for i,photo in enumerate([{}, {'url':''}, {'url':None}, {'url':'https://example.com/a.webp'}])]
        for seed in range(20):
            selected=select_products(context(),products()+no_photos,rng=random.Random(seed))['produtos']
            self.assertTrue(all(p['imagem_url'] and not p['id'].startswith('missing') for p in selected))
        with self.assertRaises(ServiceError): select_products(context(),products()[:2]+no_photos)

    def test_each_official_client_only_gets_products_matching_its_parameters(self):
        records=[]
        for occasion in ['Churrasco','Praticidade','Dia a dia','Lanches']:
            for kind in ['Carnes','Aves','Pescados','Acompanhamentos','Sobremesas']:
                for i in range(4):
                    doc_id=f'{occasion}-{kind}-{i}'
                    records.append((doc_id,dict(nome=doc_id,codigo=doc_id,disponivelNoJogo=True,
                                               tiposProduto=[kind],ocasioes=[occasion],imagemSwift=products()[0][1]['imagemSwift'])))
        for client_id, parameters in PRODUCT_PARAMETERS.items():
            client=SCENARIOS[client_id];node=client['noInicial'];history=[]
            while node != STAGES[client_id]:
                option=client['dialogo'][node]['opcoes'][0]
                history.append({'no_id':node,'opcao_id':option['id']});node=option['proximoNo']
            revealed=recommendation_context(RecommendationRequest(cliente_id=client_id,no_atual=node,historico=history))
            for seed in range(10):
                output=select_products(revealed,records,rng=random.Random(seed))
                self.assertEqual(len(output['produtos']),3)
                for product in output['produtos']:
                    self.assertIn(parameters['ocasiao'],product['ocasioes'])
                    self.assertTrue(set(parameters['tipos']) & set(product['tiposProduto']))

    def test_new_requests_draw_different_sets_from_the_same_cached_candidates(self):
        records=products()+[(str(i),dict(products()[0][1],nome='Produto '+str(i),codigo=str(i))) for i in range(4,20)]
        original=json.dumps(records)
        draws=[select_products(context(),records,rng=random.Random(seed))['produtos'] for seed in range(12)]
        self.assertGreater(len({tuple(sorted(p['id'] for p in draw)) for draw in draws}),1)
        self.assertGreater(len({p['id'] for draw in draws for p in draw}),3)
        self.assertEqual(json.dumps(records),original)

    def test_too_few_compatible_products_never_falls_back_to_unrelated_foods(self):
        unrelated=[('dessert',dict(nome='Sorvete',codigo='dessert',disponivelNoJogo=True,
                                 tiposProduto=['Sobremesas'],ocasioes=['Lanches']))]
        with self.assertRaises(ServiceError) as error: select_products(context(),products()[:2]+unrelated)
        self.assertEqual(error.exception.code,'INSUFFICIENT_PRODUCTS')

    def test_different_codes_with_same_description_do_not_increase_draw_pool(self):
        duplicate=('other-code',dict(products()[0][1],codigo='other-code'))
        with self.assertRaises(ServiceError): select_products(context(),products()[:2]+[duplicate])

    def test_package_weight_uses_description_not_commercial_sales_volume(self):
        self.assertEqual(package_weight('LINGUICA SWIFT 700G'), 0.7)
        self.assertEqual(package_weight('FILE 1KG'), 1)
        self.assertEqual(package_weight('FILE 0,5 KG'), 0.5)
        self.assertIsNone(package_weight('ACEM KG'))
        self.assertIsNone(package_weight('KIT 700G 1KG'))
        card = public_product('p', {'nome':'FILE 1KG','dadosOriginais':{'Volume (KG)':330187}})
        self.assertEqual(card['peso_embalagem_kg'], 1)
        self.assertNotIn('330187', str(card))

    def test_three_distinct_available_products_without_commercial_fields(self):
        records=products()+[('duplicate',dict(products()[0][1]))]+[('hidden',dict(products()[0][1],disponivelNoJogo=False,codigo='hidden'))]
        output=select_products(context(),records)
        self.assertEqual(len(output['produtos']),3)
        self.assertEqual(len({p['codigo'] for p in output['produtos']}),3)
        self.assertNotIn('private',str(output));self.assertNotIn('Margem',str(output))
        self.assertNotIn('hidden',str(output))

    def test_churrasco_context_prefers_churrasco_to_dessert(self):
        records=products()+[('dessert',dict(nome='Sorvete',codigo='dessert',disponivelNoJogo=True,tiposProduto=['Sobremesas'],ocasioes=['Lanches']))]
        self.assertNotIn('dessert',[p['id'] for p in select_products(context(),records)['produtos']])

    def test_unknown_stage_and_invented_history_are_rejected(self):
        with self.assertRaises(ContextError): recommendation_context(RecommendationRequest(cliente_id='cliente1',no_atual='d3'))
        with self.assertRaises(ContextError): recommendation_context(RecommendationRequest(cliente_id='cliente1',no_atual='d1'))
        with self.assertRaises(ServiceError): select_products(context(),products()[:2])

    def test_images_are_restricted_to_our_public_bucket_gateway(self):
        records=[(i,dict(p,imagemSwift={'url':'https://example.com/private?token=secret'})) for i,p in products()]
        with self.assertRaises(ServiceError) as error: select_products(context(),records)
        self.assertEqual(error.exception.code,'INSUFFICIENT_PRODUCTS')

    def test_recommendations_require_auth_and_use_only_server_catalog(self):
        choice={'cliente_id':'cliente1','no_atual':'d3','historico':[{'no_id':'d1','opcao_id':'d1-o1'},{'no_id':'d2','opcao_id':'d2-o1'}]}
        with TestClient(app) as client:
            self.assertEqual(client.post('/v1/recomendacoes',json=choice).status_code,401)
            with patch('main.authorize',AsyncMock(return_value=type('Catalog',(),{'catalog':AsyncMock(return_value=products())})())), \
                 patch('main.cached_services',side_effect=AssertionError('Selection must never access the Groq pool')) as ai:
                response=client.post('/v1/recomendacoes',json=choice,headers={'Authorization':'Bearer '+'t'*30})
                self.assertEqual(response.status_code,200);self.assertEqual(len(response.json()['produtos']),3)
                self.assertEqual(client.post('/v1/recomendacoes',json=dict(choice,produtos=products())).status_code,422)
                ai.assert_not_called()

class CatalogQueryTest(unittest.IsolatedAsyncioTestCase):
    async def test_scan_continues_past_a_first_page_without_images_and_caches_only_photo_records(self):
        base='projects/epav-game/databases/(default)/documents/produtos_swift/'
        missing=[{'document':{'name':base+str(i),'fields':{'nome':{'stringValue':'Missing'}}}} for i in range(150)]
        def value(data):
            if isinstance(data,dict): return {'mapValue':{'fields':{k:value(v) for k,v in data.items()}}}
            if isinstance(data,list): return {'arrayValue':{'values':[value(v) for v in data]}}
            if isinstance(data,bool): return {'booleanValue':data}
            return {'stringValue':data}
        found=[{'document':{'name':base+id,'fields':{k:value(v) for k,v in data.items()}}} for id,data in products()]
        transport=AsyncMock(side_effect=[HTTPResult(200,{},json.dumps(missing)),HTTPResult(200,{},json.dumps(found))])
        service=FirebaseService('public','{}',transport)
        with patch.object(service,'access_token',AsyncMock(return_value='token')):
            records=await service.catalog(context())
            self.assertEqual(len(records),4)
            self.assertEqual(await service.catalog(context()),records)
        self.assertEqual(transport.call_count,2)
        query=transport.call_args.kwargs['payload']['structuredQuery']
        self.assertEqual(query['startAt'],{'values':[{'referenceValue':base+'149'}],'before':False})
        self.assertTrue(all(data['imagemSwift']['url'] for _,data in records))
        self.assertNotIn('Margem',str(service.cache.memory))

    async def test_catalog_queries_indexed_flags_and_caches_with_server_projection(self):
        docs=[{'document':{'name':'projects/epav-game/databases/(default)/documents/produtos_swift/p'+str(i),
                          'fields':{'nome':{'stringValue':'Produto '+str(i)},'disponivelNoJogo':{'booleanValue':True},
                                    'tiposProduto':{'arrayValue':{'values':[{'stringValue':'Carnes'}]}},
                                    'ocasioes':{'arrayValue':{'values':[{'stringValue':'Dia a dia'}]}},
                                    'imagemSwift':{'mapValue':{'fields':{'url':{'stringValue':products()[0][1]['imagemSwift']['url']}}}}}}} for i in range(150)]
        transport=AsyncMock(return_value=HTTPResult(200,{},json.dumps(docs)))
        service=FirebaseService('public','{}',transport)
        with patch.object(service,'access_token',AsyncMock(return_value='token')):
            records=await service.catalog();self.assertEqual(len(records),150)
            self.assertEqual(await service.catalog(),records)
        self.assertEqual(transport.call_count,1)
        query=transport.call_args.kwargs['payload']['structuredQuery']
        self.assertEqual(query['where']['compositeFilter']['filters'][0]['fieldFilter']['value'],{'booleanValue':True})
        self.assertTrue(all('Margem' not in field['fieldPath'] for field in query['select']['fields']))

    async def test_only_queries_the_configured_occasion_and_reuses_it_for_same_profile(self):
        transport=AsyncMock(return_value=HTTPResult(200,{},'[]'))
        service=FirebaseService('public','{}',transport)
        with patch.object(service,'access_token',AsyncMock(return_value='token')):
            for client_id in STAGES:
                await service.catalog({'cliente':{'id':client_id}})
        self.assertEqual(transport.call_count,3)
        facets=[call.kwargs['payload']['structuredQuery']['where']['compositeFilter']['filters'][1]
                ['fieldFilter']['field']['fieldPath'] for call in transport.call_args_list]
        self.assertEqual(set(facets),{'`dadosOriginais`.`Churrasco`','`dadosOriginais`.`Praticidade`','`dadosOriginais`.`Dia a dia`'})
