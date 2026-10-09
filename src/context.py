from models import EvaluationRequest
from scenarios import SCENARIOS
from scenarios_v2 import SCENARIOS_V2

class ContextError(ValueError):
    pass

def build_context(choice: EvaluationRequest, product: dict):
    client = (SCENARIOS_V2 if choice.roteiro == 'ia-v2' else SCENARIOS).get(choice.cliente_id)
    if not client:
        raise ContextError('Cliente desconhecido.')
    current = client['noInicial']
    history, facts = [], {}
    satisfaction = client.get('satisfacaoInicial', 50)
    for decision in choice.historico:
        if decision.no_id != current or current not in client['dialogo']:
            raise ContextError('O histórico não segue a sequência do atendimento.')
        node = client['dialogo'][current]
        option = next((o for o in node['opcoes'] if o['id'] == decision.opcao_id), None)
        if not option:
            raise ContextError('Opção desconhecida no histórico.')
        if node.get('descoberta'):
            facts[node['descoberta']['chave']] = node['descoberta']['rotulo']
        text = node['texto']
        selection = option['texto']
        returning = node.get('retorno')
        if returning and returning['chave'] in facts:
            text += '\n' + returning['texto']
            if option['qualidade'] == 'excelente':
                selection = returning['opcaoExcelente']
        history.append(dict(id=current, fala_cliente=text, escolha_jogador=selection,
                            resposta_cliente=option.get('resposta', '')))
        if choice.roteiro == 'ia-v2':
            if node.get('aberturaVendedor'):
                history[-1]['abertura_vendedor'] = node['aberturaVendedor']
            satisfaction = max(0, min(100, satisfaction + option['efeitoSatisfacao']))
            if option['pontos'] <= -6 and satisfaction < 15:
                raise ContextError('O cliente já encerrou este atendimento.')
        current = option['proximoNo']
    if current != choice.no_atual or current not in client['dialogo']:
        raise ContextError('Etapa atual incompatível com o histórico.')
    node = client['dialogo'][current]
    if node.get('descoberta'):
        facts[node['descoberta']['chave']] = node['descoberta']['rotulo']
    text = node['texto']
    returning = node.get('retorno')
    if returning and returning['chave'] in facts:
        text += '\n' + returning['texto']
    history.append(dict(id=current, fala_cliente=text))
    # Product fields come exclusively from the server-side catalog, never from the player.
    customer = dict(id=client['id'], nome=client['nome'], perfil=client['perfil'])
    if choice.roteiro == 'ia-v2':
        # These are declared preferences, not allergies or medical restrictions.
        customer['preferencias_alimentares'] = client['restricoes']
        customer['alergia_declarada'] = False
    return dict(cliente=customer,
                conversa=history, ficha_escuta=facts, produto=product,
                quantidade=choice.quantidade.model_dump() if choice.quantidade else None,
                observacao_jogador=choice.observacao_jogador)
