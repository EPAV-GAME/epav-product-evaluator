import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from models import RecommendationRequest
from context import ContextError
from recommendations import recommendation_context, select_products, package_weight, public_product
from services import FirebaseService, HTTPResult, ServiceError
from main import app
import json

def context():
    return recommendation_context(RecommendationRequest(cliente_id='cliente1',no_atual='d3',historico=[
        {'no_id':'d1','opcao_id':'d1-o1'},{'no_id':'d2','opcao_id':'d2-o1'}]))

def products():
    data = dict(disponivelNoJogo=True,tiposProduto=['Carnes'],ocasioes=['Churrasco'],
                dadosOriginais={'Marca':'Swift','Margem':99,'Nome Fornecedor':'private'})
    return [(str(i),dict(data,nome='Produto '+str(i),codigo=str(i))) for i in range(4)]

class RecommendationTest(unittest.TestCase):
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
        self.assertTrue(all(p['imagem_url'] is None for p in select_products(context(),records)['produtos']))

    def test_recommendations_require_auth_and_use_only_server_catalog(self):
        choice={'cliente_id':'cliente1','no_atual':'d3','historico':[{'no_id':'d1','opcao_id':'d1-o1'},{'no_id':'d2','opcao_id':'d2-o1'}]}
        with TestClient(app) as client:
            self.assertEqual(client.post('/v1/recomendacoes',json=choice).status_code,401)
            with patch('main.authorize',AsyncMock(return_value=type('Catalog',(),{'catalog':AsyncMock(return_value=products())})())):
                response=client.post('/v1/recomendacoes',json=choice,headers={'Authorization':'Bearer '+'t'*30})
                self.assertEqual(response.status_code,200);self.assertEqual(len(response.json()['produtos']),3)
                self.assertEqual(client.post('/v1/recomendacoes',json=dict(choice,produtos=products())).status_code,422)

class CatalogQueryTest(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_queries_indexed_flags_and_caches_with_server_projection(self):
        docs=[{'document':{'name':'projects/epav-game/databases/(default)/documents/produtos_swift/p'+str(i),
                          'fields':{'nome':{'stringValue':'Produto'},'disponivelNoJogo':{'booleanValue':True}}}} for i in range(150)]
        transport=AsyncMock(return_value=HTTPResult(200,{},json.dumps(docs)))
        service=FirebaseService('public','{}',transport)
        with patch.object(service,'access_token',AsyncMock(return_value='token')):
            records=await service.catalog();self.assertEqual(len(records),600)
            self.assertEqual(await service.catalog(),records)
        self.assertEqual(transport.call_count,4)
        query=transport.call_args.kwargs['payload']['structuredQuery']
        self.assertEqual(query['where']['compositeFilter']['filters'][0]['fieldFilter']['value'],{'booleanValue':True})
        self.assertTrue(all('Margem' not in field['fieldPath'] for field in query['select']['fields']))
