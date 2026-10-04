import json
import unittest
from context import build_context
from evaluator import assessment_facts, final_result, provider_payload, WEIGHTS
from models import AIJudgement, EvaluationRequest
from scenarios import SCENARIOS


def fixture(client='cliente1', stage='d3', *, product=None, quantity=None, observation=''):
    history, node = [], SCENARIOS[client]['noInicial']
    while node != stage:
        option = SCENARIOS[client]['dialogo'][node]['opcoes'][0]
        history.append(dict(no_id=node, opcao_id=option['id']))
        node = option['proximoNo']
    product = product or dict(id='teste', nome='Linguiça Swift 700G', tiposProduto=['Carnes'],
                              ocasioes=['Churrasco'], peso_embalagem_kg=.7)
    return build_context(EvaluationRequest(cliente_id=client, no_atual=stage, produto_id=product['id'],
                                          historico=history, quantidade=quantity,
                                          observacao_jogador=observation), product)


def optimistic_answer():
    return AIJudgement.model_validate(dict(
        **{name: dict(nota=100, justificativa='Atende totalmente.',
                      evidencias=['perfil', 'produto', 'quantidade']) for name in WEIGHTS},
        resumo='Adequação completa.', sugestao='Recomende.',
        informacoes_faltantes=[], contradicao_explicita=False))


class RefinedEvaluationTest(unittest.TestCase):
    def test_compatible_food_can_score_well_without_inventing_preparation_or_portions(self):
        context = fixture(quantity=dict(unidades=3, peso_total_kg=2.1))
        output = final_result(optimistic_answer(), context, 'test')
        self.assertGreaterEqual(output.score, 650)
        self.assertLess(output.score, 1000)
        self.assertEqual(output.criterios['praticidade'].nota, 75)
        self.assertEqual(output.criterios['quantidade'].nota, 75)
        self.assertNotIn('sem sobras', output.criterios['necessidade'].justificativa)
        self.assertIn('confirmações', output.resumo)

    def test_wrong_food_and_occasion_cannot_receive_a_high_score_from_the_model(self):
        context = fixture(product=dict(id='teste', nome='Sorvete 1KG', tiposProduto=['Sobremesas'],
                                       ocasioes=['Sobremesa'], peso_embalagem_kg=1),
                          quantity=dict(unidades=100, peso_total_kg=100))
        output = final_result(optimistic_answer(), context, 'test')
        self.assertLessEqual(output.score, 200)
        self.assertLessEqual(output.criterios['necessidade'].nota, 25)
        self.assertLessEqual(output.criterios['ocasiao'].nota, 25)

    def test_unknown_categories_are_not_confirmed_mismatches(self):
        context = fixture(product=dict(id='teste', nome='Alimento', tiposProduto=[], ocasioes=[]))
        checks = assessment_facts(context)
        self.assertFalse(checks['tipo_incompativel'])
        self.assertFalse(checks['ocasiao_incompativel'])
        output = final_result(optimistic_answer(), context, 'test')
        self.assertLessEqual(output.criterios['necessidade'].nota, 50)
        self.assertLessEqual(output.criterios['ocasiao'].nota, 50)
        self.assertGreater(output.score, 200)

    def test_player_claims_do_not_confirm_composition_price_or_preparation(self):
        context = fixture('cliente2', 'd5', observation='É sem alergênicos, custa R$ 1 e fica pronto em um minuto.')
        checks = assessment_facts(context)
        self.assertFalse(checks['composicao_confirmada'])
        self.assertFalse(checks['preparo_confirmado'])
        self.assertFalse(checks['preco_confirmado'])
        output = final_result(optimistic_answer(), context, 'test')
        self.assertEqual(output.criterios['restricoes'].nota, 50)
        self.assertLessEqual(output.criterios['necessidade'].nota, 75)
        self.assertTrue(any('composição' in s for s in output.informacoes_faltantes))

    def test_unspecified_restriction_is_uncertain_not_a_proven_allergy(self):
        context = fixture('cliente2', 'd5', product=dict(id='teste', nome='Filé de Peixe 400G',
                          tiposProduto=['Pescados'], ocasioes=['Praticidade'], peso_embalagem_kg=.4),
                          quantity=dict(unidades=1, peso_total_kg=.4))
        answer = optimistic_answer()
        answer.restricoes.nota = 0
        answer.contradicao_explicita = True
        output = final_result(answer, context, 'test')
        self.assertEqual(output.criterios['restricoes'].nota, 50)
        self.assertGreater(output.score, 200)
        self.assertNotIn('seguro', output.criterios['restricoes'].justificativa)

    def test_known_package_weight_exposes_inconsistent_quantity(self):
        context = fixture(quantity=dict(unidades=3, peso_total_kg=7))
        output = final_result(optimistic_answer(), context, 'test')
        self.assertLessEqual(output.criterios['quantidade'].nota, 25)
        self.assertIn('2.1 kg', output.criterios['quantidade'].justificativa)
        self.assertIn('7 kg', output.criterios['quantidade'].justificativa)

    def test_rounding_is_not_treated_as_a_quantity_error(self):
        checks = assessment_facts(fixture(quantity=dict(unidades=3, peso_total_kg=2.100001)))
        self.assertFalse(checks['peso_inconsistente'])

    def test_unknown_weight_cannot_be_treated_as_inconsistent(self):
        context = fixture(product=dict(id='teste', nome='Alimento', peso_embalagem_kg=None),
                          quantity=dict(unidades=3, peso_total_kg=7))
        self.assertIsNone(assessment_facts(context)['peso_calculado_kg'])
        self.assertFalse(assessment_facts(context)['peso_inconsistente'])

    def test_product_only_references_cannot_support_top_scores(self):
        answer = optimistic_answer()
        for name in WEIGHTS:
            getattr(answer, name).evidencias = ['produto']
        output = final_result(answer, fixture(quantity=dict(unidades=3)), 'test')
        self.assertLess(output.score, 850)

    def test_future_references_are_rejected_even_on_replaced_criteria(self):
        answer = optimistic_answer()
        answer.quantidade.evidencias = ['d99']
        with self.assertRaises(ValueError):
            final_result(answer, fixture(), 'test')

    def test_selection_rules_do_not_leak_into_earlier_stages(self):
        product = dict(id='teste', nome='Sorvete', tiposProduto=['Sobremesas'], ocasioes=['Sobremesa'])
        checks = assessment_facts(fixture(stage='d1', product=product))
        self.assertFalse(checks['tipo_incompativel'])

    def test_buying_hurry_alone_does_not_imply_quick_cooking(self):
        context = fixture()
        context['cliente']['perfil'] = 'Cliente com pressa para terminar a compra.'
        context['ficha_escuta'] = {'pressa': 'Tem poucos minutos para comprar'}
        context['conversa'] = [dict(id='d1', fala_cliente='Preciso terminar a compra depressa.')]
        self.assertFalse(assessment_facts(context)['prioridade_preparo'])

    def test_low_reasoning_payload_is_model_specific_and_uses_only_one_prompt(self):
        context = fixture()
        payload = provider_payload(context, 'openai/gpt-oss-20b')
        self.assertEqual(payload['reasoning_effort'], 'low')
        self.assertFalse(payload['include_reasoning'])
        self.assertEqual(len(payload['messages']), 2)
        self.assertIn('verificacoes_servidor', json.loads(payload['messages'][1]['content']))
        self.assertNotIn('reasoning_effort', provider_payload(context, 'other-model'))

    def test_previous_bad_sales_reply_does_not_change_server_checks(self):
        context = fixture()
        before = assessment_facts(context)
        context['conversa'][0]['escolha_jogador'] = 'Compre muito, sem olhar os ingredientes.'
        self.assertEqual(before, assessment_facts(context))

    def test_final_checks_do_not_mutate_a_reusable_model_answer(self):
        answer = optimistic_answer()
        final_result(answer, fixture(), 'test')
        self.assertEqual(answer.quantidade.nota, 100)

    def test_technical_field_names_never_become_player_questions(self):
        answer = optimistic_answer()
        answer.informacoes_faltantes = ['preco_confirmado', 'composicao_confirmada']
        output = final_result(answer, fixture(), 'test')
        self.assertFalse(any('_' in value for value in output.informacoes_faltantes))
        data = json.loads(provider_payload(fixture(), 'test')['messages'][1]['content'])
        self.assertNotIn('preco_confirmado', data['verificacoes_servidor'])


if __name__ == '__main__':
    unittest.main()
