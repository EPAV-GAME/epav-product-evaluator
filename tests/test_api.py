import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock,patch
from fastapi.testclient import TestClient
from pydantic import ValidationError
from models import AIJudgement,EvaluationRequest
from context import build_context,ContextError
from evaluator import final_result,provider_payload,WEIGHTS
from services import GroqPool,HTTPResult,ServiceError,FirebaseService,parse_keys
from main import app

def judgement(score=80):
    return {**{name:dict(nota=score,justificativa='Compatível com os fatos.',evidencias=['produto']) for name in WEIGHTS},
            'resumo':'Uma escolha coerente.','sugestao':'Confirme a quantidade necessária.',
            'informacoes_faltantes':[],'contradicao_explicita':False}

def result(status=200,payload=None,headers=None):
    body = payload if payload is not None else dict(choices=[dict(message=dict(content=json.dumps(judgement())))])
    return HTTPResult(status,headers or {},json.dumps(body))

class EvaluationTest(unittest.TestCase):
    def test_history_and_facts_are_reconstructed_without_future_nodes(self):
        choice=EvaluationRequest(cliente_id='cliente1',no_atual='d3',produto_id='p',
                                 historico=[dict(no_id='d1',opcao_id='d1-o1'),dict(no_id='d2',opcao_id='d2-o1')])
        context=build_context(choice,dict(id='p',nome='Linguiça Swift'))
        self.assertEqual([turn['id'] for turn in context['conversa']],['d1','d2','d3'])
        self.assertEqual(len(context['ficha_escuta']),3)
        self.assertNotIn('d4',str(context))

    def test_fake_and_out_of_order_history_is_rejected(self):
        choice=EvaluationRequest(cliente_id='cliente1',no_atual='d3',produto_id='p',
                                 historico=[dict(no_id='d2',opcao_id='d2-o1')])
        with self.assertRaises(ContextError): build_context(choice,{})
        with self.assertRaises(ValidationError):
            EvaluationRequest(cliente_id='cliente1',no_atual='d1',produto_id='p',score=1000)

    def test_scores_are_bounded_and_recomputed(self):
        context=build_context(EvaluationRequest(cliente_id='cliente1',no_atual='d1',produto_id='p',quantidade=dict(unidades=2)),dict(id='p'))
        for note,expected in [(0,150),(80,638),(100,638)]:
            output=final_result(AIJudgement.model_validate(judgement(note)),context,'test')
            self.assertEqual(output.score,expected)
            self.assertEqual(output.percentual_adequacao,expected/10)
        invalid=judgement(1001)
        with self.assertRaises(ValidationError): AIJudgement.model_validate(invalid)
        conflict=judgement(100); conflict['contradicao_explicita']=True
        # The provider flag is insufficient proof of an ingredient conflict.
        self.assertEqual(final_result(AIJudgement.model_validate(conflict),context,'test').score,638)

    def test_model_cannot_cite_future_turns(self):
        context=build_context(EvaluationRequest(cliente_id='cliente1',no_atual='d1',produto_id='p'),dict(id='p'))
        answer=judgement();answer['necessidade']['evidencias']=['d8']
        with self.assertRaises(ValueError): final_result(AIJudgement.model_validate(answer),context,'test')
        self.assertEqual(provider_payload(context,'test')['response_format']['json_schema']['strict'],True)

class GroqTest(unittest.IsolatedAsyncioTestCase):
    async def test_quota_switches_to_next_key(self):
        transport=AsyncMock(side_effect=[result(429,{}, {'retry-after':'60'}),result()])
        first,second='gsk_'+'a'*30,'gsk_'+'b'*30
        pool=GroqPool(first+'|'+second,transport,clock=lambda:100)
        await pool.complete({})
        self.assertEqual(transport.call_count,2)
        self.assertEqual(transport.call_args.kwargs['headers']['Authorization'],'Bearer '+second)
        transport.reset_mock();transport.side_effect=[result()]
        await pool.complete({})
        self.assertEqual(transport.call_args.kwargs['headers']['Authorization'],'Bearer '+second)

    async def test_exhausted_keys_report_unavailable_without_fake_score(self):
        transport=AsyncMock(return_value=result(429,{'error':{'message':'secret'}},{'retry-after':'120'}))
        pool=GroqPool('gsk_'+'a'*30,transport,clock=lambda:100)
        with self.assertRaises(ServiceError) as error: await pool.complete({})
        self.assertEqual(error.exception.status,503)
        self.assertEqual(error.exception.retry_after,120)
        self.assertNotIn('secret',str(error.exception))

    async def test_bad_model_request_is_not_retried_across_keys(self):
        transport=AsyncMock(return_value=result(400,{}))
        pool=GroqPool('gsk_'+'a'*30+'|'+'gsk_'+'b'*30,transport)
        with self.assertRaises(ServiceError): await pool.complete({})
        self.assertEqual(transport.call_count,1)

    async def test_invalid_ai_json_is_rejected(self):
        pool=GroqPool('gsk_'+'a'*30,AsyncMock(return_value=result(200,dict(choices=[dict(message=dict(content='{}'))]))))
        with self.assertRaises(ServiceError) as error: await pool.complete({})
        self.assertEqual(error.exception.code,'INVALID_AI_RESPONSE')

    def test_duplicates_and_placeholders(self):
        key='gsk_'+'a'*30
        self.assertEqual(parse_keys('{'+key+'}|'+key),[key])
        with self.assertRaises(ServiceError): parse_keys('chave1|chave2')

    async def test_private_catalog_fields_never_reach_ai(self):
        data=dict(nome={'stringValue':'Produto'},disponivelNoJogo={'booleanValue':True},
                  dadosOriginais={'mapValue':{'fields':{'Marca':{'stringValue':'Swift'},'Margem':{'doubleValue':99},'Nome Fornecedor':{'stringValue':'private'}}}})
        firebase=FirebaseService('public','{}',AsyncMock(return_value=result(200,dict(fields=data))))
        with patch.object(firebase,'access_token',AsyncMock(return_value='token')):
            product=await firebase.product('p')
        self.assertNotIn('private',str(product));self.assertNotIn('Margem',str(product))

class APIAuthTest(unittest.TestCase):
    def test_openapi_documents_bearer_authentication(self):
        schema=app.openapi()
        self.assertEqual(schema['components']['securitySchemes']['HTTPBearer']['scheme'],'bearer')
        self.assertEqual(schema['paths']['/v1/avaliacoes']['post']['security'],[{'HTTPBearer':[]}])

    def test_anonymous_requests_are_rejected(self):
        with TestClient(app) as client:
            response=client.post('/v1/avaliacoes',json=dict(cliente_id='cliente1',no_atual='d1',produto_id='p'))
            self.assertEqual(response.status_code,401)
            self.assertNotIn('GROQ',response.text)

    def test_large_body_is_rejected_before_auth_or_provider(self):
        with TestClient(app) as client:
            self.assertEqual(client.post('/v1/avaliacoes',content='x'*32769).status_code,413)

    def test_authenticated_evaluation_uses_server_product(self):
        env=dict(GROQ_API_KEYS='gsk_'+'a'*30,FIREBASE_SERVICE_ACCOUNT_JSON='{}',FIREBASE_WEB_API_KEY='public',GROQ_MODEL='test')
        pool,firebase=AsyncMock(),AsyncMock()
        pool.complete.return_value=AIJudgement.model_validate(judgement())
        firebase.product.return_value=dict(id='p',nome='Produto do servidor')
        with patch.dict(os.environ,env), patch('services.FirebaseService.authenticate',AsyncMock(return_value='uid')), \
             patch('main.cached_services',return_value=(pool,firebase)),TestClient(app) as client:
            response=client.post('/v1/avaliacoes',headers={'Authorization':'Bearer '+'x'*30},json=dict(cliente_id='cliente1',no_atual='d1',produto_id='p'))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['score'],613)
        self.assertIn('groq;dur=',response.headers['Server-Timing'])
        self.assertEqual(pool.complete.call_count,1)
        self.assertNotIn('uid',str(pool.complete.call_args))
        self.assertIn('Produto do servidor',str(pool.complete.call_args))

if __name__=='__main__': unittest.main()
