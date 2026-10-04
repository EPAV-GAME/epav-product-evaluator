"""Three distinct catalog products selected from the revealed customer context."""
import random
import re
import unicodedata
from context import build_context, ContextError
from models import EvaluationRequest

STAGES = {'cliente1': 'd3', 'cliente2': 'd5', 'cliente3': 'd7', 'cliente4': 'd6', 'cliente5': 'd7'}

# Requirements taken from the five official scripts, not inferred by an AI.
# These are selection rules, not measured customer statistics.
PRODUCT_PARAMETERS = {
    'cliente1': {'ocasiao': 'Churrasco', 'tipos': ('Carnes', 'Aves')},
    'cliente2': {'ocasiao': 'Praticidade', 'tipos': ('Carnes', 'Aves', 'Pescados', 'Acompanhamentos')},
    'cliente3': {'ocasiao': 'Dia a dia', 'tipos': ('Carnes',)},
    'cliente4': {'ocasiao': 'Praticidade', 'tipos': ('Carnes', 'Aves', 'Pescados', 'Acompanhamentos')},
    'cliente5': {'ocasiao': 'Dia a dia', 'tipos': ('Carnes', 'Aves', 'Pescados', 'Acompanhamentos')},
}


def product_parameters(context):
    parameters = PRODUCT_PARAMETERS.get(context.get('cliente', {}).get('id'))
    if parameters is None:
        raise ContextError('Cliente desconhecido para seleção de produtos.')
    return parameters

def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFD', str(text).lower()) if unicodedata.category(c) != 'Mn')

def recommendation_context(choice):
    if STAGES.get(choice.cliente_id) != choice.no_atual:
        raise ContextError('Esta etapa não é uma recomendação de produtos.')
    return build_context(EvaluationRequest(**choice.model_dump(), produto_id='catalogo'), {})

def image_url(value):
    return value if isinstance(value, str) and re.fullmatch(
        r'https://epav-swift-images\.kevinernandes2012\.workers\.dev/images/swift/[a-f0-9]{64}\.webp', value) else None

def package_weight(name):
    # The spreadsheet's Volume (KG) is aggregate sales volume, not package weight.
    matches = re.findall(r'(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(KG|G)\b', str(name).upper())
    if len(matches) != 1: return None
    number, unit = matches[0]
    weight = float(number.replace(',', '.')) / (1000 if unit == 'G' else 1)
    return weight if 0 < weight <= 30 else None

def public_product(doc_id, data):
    original = data.get('dadosOriginais') or {}
    def field(key):
        value = original.get(key)
        return str(value)[:240] if value is not None and value != '' else None
    return dict(id=doc_id, codigo=str(data.get('codigo', ''))[:40], nome=str(data.get('nome', ''))[:240],
                tiposProduto=[str(v)[:60] for v in data.get('tiposProduto', [])][:6],
                ocasioes=[str(v)[:60] for v in data.get('ocasioes', [])][:8],
                marca=field('Marca'), formato=field('Formato'), unidade_medida=field('Unidade Medida'),
                peso_embalagem_kg=package_weight(data.get('nome', '')), imagem_url=image_url((data.get('imagemSwift') or {}).get('url')))

def select_products(context, records, *, rng=None):
    parameters = product_parameters(context)
    candidates = []
    for doc_id, data in records:
        if data.get('disponivelNoJogo') is not True or not data.get('nome'): continue
        product = public_product(doc_id, data)
        if parameters['ocasiao'] not in product['ocasioes']: continue
        if not any(t in parameters['tipos'] for t in product['tiposProduto']): continue
        candidates.append(product)
    # Prefer an existing photo when choosing which duplicate row represents the food.
    # All distinct compatible foods remain eligible for the random draw.
    candidates.sort(key=lambda p: (not bool(p['imagem_url']), p['id']))
    pool, names, codes, identifiers = [], set(), set(), set()
    for product in candidates:
        name = normalize(product['nome'])
        code = normalize(product['codigo'].strip())
        if name in names or (code and code in codes) or product['id'] in identifiers: continue
        names.add(name); identifiers.add(product['id'])
        if code: codes.add(code)
        pool.append(product)
    if len(pool) < 3:
        from services import ServiceError
        raise ServiceError('INSUFFICIENT_PRODUCTS',503)
    # Cache the candidate catalog, not the draw. Each request samples without replacement.
    chosen = (rng if rng is not None else random.SystemRandom()).sample(pool, 3)
    return dict(cliente_id=context['cliente']['id'], no_atual=context['conversa'][-1]['id'],
                produtos=chosen, ficha_escuta=list(context['ficha_escuta'].values()),
                orientacao='Compare as fichas com as necessidades do cliente. Confirme composição e restrições antes de recomendar.')
