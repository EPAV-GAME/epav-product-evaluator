import unittest
from unittest.mock import AsyncMock,patch
from fastapi.testclient import TestClient
from context import build_context,SCENARIOS_V2,ContextError
from models import EvaluationRequest,AIJudgement
from recommendations import recommendation_context
from models import RecommendationRequest
from menu import MENU_STAGES
from evaluator import assessment_facts,final_result
from main import app

def payload(client,stage,option=1):
    script=SCENARIOS_V2[client]
    nodes=list(script['dialogo']);end=nodes.index(stage)
    return dict(roteiro='ia-v2',cliente_id=client,no_atual=stage,
        historico=[dict(no_id=n,opcao_id=f'{n}-o{option}') for n in nodes[:end]])

class RevisedScriptsTest(unittest.TestCase):
    def test_all_new_ids_and_meal_stages_are_valid_without_future_conversation(self):
        for client,script in SCENARIOS_V2.items():
            self.assertTrue(script['dialogo']['d1']['aberturaVendedor'])
            for category,stage in MENU_STAGES[client].items():
                for option in (1,5,6):
                    context=recommendation_context(RecommendationRequest(**payload(client,stage,option),categoria=category))
                    self.assertEqual(context['conversa'][-1]['id'],stage)
                    self.assertNotIn('d'+str(int(stage[1:])+1),[turn['id'] for turn in context['conversa']])
                    self.assertEqual(context['conversa'][0]['abertura_vendedor'],script['dialogo']['d1']['aberturaVendedor'])
                    self.assertFalse(assessment_facts(dict(context,quantidade=None,produto={'nome':'Arroz'}))['restricao_alimentar'])

    def test_new_ids_do_not_change_the_legacy_contract(self):
        history=[dict(no_id='d1',opcao_id='d1-o8')]
        revised=EvaluationRequest(roteiro='ia-v2',cliente_id='cliente1',no_atual='d2',produto_id='p',historico=history)
        build_context(revised,{})
        with self.assertRaises(ContextError):build_context(revised.model_copy(update={'roteiro':'legado'}),{})

    def test_history_after_a_very_bad_early_close_is_rejected(self):
        choice=EvaluationRequest(**payload('cliente4','d6',8),produto_id='p')
        with self.assertRaises(ContextError):build_context(choice,{})

    def test_preferences_are_not_allergies_and_proven_conflicts_cap_the_score(self):
        for client,name in [('cliente2','Picanha bovina Swift 1KG'),('cliente4','Batata pre frita Swift 400G')]:
            context=build_context(EvaluationRequest(**payload(client,'d6'),produto_id='p',quantidade={'unidades':1}),
                dict(id='p',nome=name,tiposProduto=['Carnes'],ocasioes=['Praticidade'],peso_embalagem_kg=1))
            self.assertFalse(assessment_facts(context)['restricao_alimentar'])
            self.assertTrue(assessment_facts(context)['preferencia_conflitante'])
            answer=AIJudgement.model_validate({**{key:dict(nota=100,justificativa='Relaciona perfil e produto.',evidencias=['perfil','produto','quantidade'])
                for key in ['necessidade','ocasiao','praticidade','restricoes','quantidade']},
                'resumo':'Boa escolha','sugestao':'Confira o preparo','informacoes_faltantes':[]})
            result=final_result(answer,context,'test')
            self.assertLessEqual(result.score,250);self.assertEqual(result.criterios['restricoes'].nota,0)

    def test_exact_code_fiches_require_auth_and_do_not_expose_private_data_or_use_groq(self):
        photo='https://epav-swift-images.kevinernandes2012.workers.dev/images/swift/'+'a'*64+'.webp'
        record={'nome':'Pão de alho','codigo':'616673','imagemSwift':{'url':photo},'margem':90,'fornecedor':'privado'}
        fake=type('DB',(),{'_menu_catalog':AsyncMock(return_value=[('id-real',record)])})()
        with TestClient(app) as client:
            self.assertEqual(client.post('/v1/catalogo/itens',json={'codigos':['616673']}).status_code,401)
            with patch('main.authorize',AsyncMock(return_value=fake)),patch('main.cached_services',side_effect=AssertionError('Groq not allowed')):
                response=client.post('/v1/catalogo/itens',json={'codigos':['616673','999999']},headers={'Authorization':'Bearer '+'t'*30})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['codigos_sem_foto'],['999999'])
                self.assertEqual(response.json()['produtos'][0]['id'],'id-real')
                self.assertNotIn('margem',response.text);self.assertNotIn('fornecedor',response.text)
                self.assertEqual(client.post('/v1/catalogo/itens',json={'codigos':['https://evil.example']}).status_code,422)
