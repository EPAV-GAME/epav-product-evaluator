"""Meal roles and dialogue checkpoints. No AI or invented catalog entries."""
import re
from context import ContextError
from recommendations import normalize
from scenarios import SCENARIOS

CATEGORIES = ('entrada', 'principal', 'acompanhamento', 'bebida', 'sobremesa')
LABELS = dict(zip(CATEGORIES, ('Entrada', 'Prato principal', 'Acompanhamento', 'Bebidas', 'Sobremesa')))
MENU_STAGES = {client: dict(zip(CATEGORIES, list(script['dialogo'])[-5:]))
               for client, script in SCENARIOS.items()}

def validate_stage(client, node, category):
    if MENU_STAGES.get(client, {}).get(category) != node:
        raise ContextError('Categoria incompatível com esta etapa do diálogo.')

def roles(product):
    name = normalize(product.get('nome', ''))
    types = set(product.get('tiposProduto', []))
    if 'Sobremesas' in types or re.search(r'\b(sorvete|sobremesa|pudim|brownie|petit gateau|torta doce|acai|churros)\b', name):
        return {'sobremesa'}
    if re.match(r'^(?:swift\s+)?(?:suco|refrigerante|cerveja|vinho|agua(?: mineral)?|cha|bebida|espumante|cafe)\b', name):
        return {'bebida'}
    found = set()
    if re.match(r'^(?:(?:mini|swift)\s+)?(?:pao de alho|bolinho|croquete|coxinha(?! da asa)|empanada|pastel|esfiha|quibe|kibe|salgadinho|dadinho|bruschetta|queijo coalho|nuggets?)\b', name):
        found.add('entrada')
    if 'Acompanhamentos' in types or re.search(r'\b(arroz|feijao|farofa|batata|legumes|brocolis|mandioca|salada|polenta|pure|cuscuz)\b', name):
        found.add('acompanhamento')
    if (types & {'Carnes', 'Aves', 'Pescados'} and not found) or re.search(r'\b(lasanha|pizza|macarrao|massa recheada)\b', name):
        found.add('principal')
    return found

def selection_context(choice, context):
    if choice.categoria:
        validate_stage(choice.cliente_id, choice.no_atual, choice.categoria)
        previous = [item.categoria for item in choice.escolhas_anteriores]
        index = CATEGORIES.index(choice.categoria)
        if len(set(previous)) != len(previous) or any(CATEGORIES.index(c) >= index for c in previous):
            raise ContextError('As escolhas anteriores devem preceder a categoria atual, sem repetição.')
        if previous != sorted(previous, key=CATEGORIES.index):
            raise ContextError('Escolhas anteriores fora de ordem.')
        context['categoria_refeicao'] = choice.categoria
        context['papel_refeicao'] = LABELS[choice.categoria]
    elif choice.escolhas_anteriores:
        raise ContextError('Informe a categoria para avaliar uma refeição.')
    return context

async def previous_context(choice, firebase):
    import asyncio
    read=getattr(firebase,'menu_product',firebase.product) if choice.categoria else firebase.product
    products = await asyncio.gather(*(read(item.produto_id) for item in choice.escolhas_anteriores))
    return [dict(categoria=item.categoria, produto=product, quantidade=item.quantidade.model_dump())
            for item, product in zip(choice.escolhas_anteriores, products)]
