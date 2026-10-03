"""Three distinct catalog products selected from the revealed customer context."""
import hashlib
import re
import unicodedata
from context import build_context, ContextError
from models import EvaluationRequest

STAGES = {'cliente1': 'd3', 'cliente2': 'd5', 'cliente3': 'd7', 'cliente4': 'd6', 'cliente5': 'd7'}

def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFD', str(text).lower()) if unicodedata.category(c) != 'Mn')

def recommendation_context(choice):
    if STAGES.get(choice.cliente_id) != choice.no_atual:
        raise ContextError('Esta etapa não é uma recomendação de produtos.')
    return build_context(EvaluationRequest(**choice.model_dump(), produto_id='catalogo'), {})

def image_url(value):
    return value if isinstance(value, str) and re.fullmatch(
        r'https://epav-swift-images\.kevinernandes2012\.workers\.dev/images/swift/[a-f0-9]{64}\.webp', value) else None

def public_product(doc_id, data):
    original = data.get('dadosOriginais') or {}
    def field(key):
        value = original.get(key)
        return str(value)[:240] if value is not None and value != '' else None
    return dict(id=doc_id, codigo=str(data.get('codigo', ''))[:40], nome=str(data.get('nome', ''))[:240],
                tiposProduto=[str(v)[:60] for v in data.get('tiposProduto', [])][:6],
                ocasioes=[str(v)[:60] for v in data.get('ocasioes', [])][:8],
                marca=field('Marca'), formato=field('Formato'), unidade_medida=field('Unidade Medida'),
                volume_kg=field('Volume (KG)'), imagem_url=image_url((data.get('imagemSwift') or {}).get('url')))

def select_products(context, records):
    # Only the official profile and already revealed facts/falas contribute to retrieval.
    text = normalize(context['cliente']['perfil'] + ' ' + ' '.join(context['ficha_escuta'].values()) +
                     ' ' + ' '.join(turn['fala_cliente'] for turn in context['conversa']))
    occasions = {'Dia a dia': 2}
    if 'churrasco' in text: occasions['Churrasco'] = 12
    if any(word in text for word in ['pressa', 'pratic', 'tempo', 'cansad']): occasions['Praticidade'] = 5
    if any(word in text for word in ['pessoas', 'familia', 'semana']): occasions['Família'] = 3
    types = {'Carnes': 4, 'Aves': 4, 'Pescados': 3}
    if 'churrasco' in text or 'carnes' in text: types = {'Carnes': 10, 'Aves': 3}
    unique = {}
    for doc_id, data in records:
        if data.get('disponivelNoJogo') is not True or not data.get('nome'): continue
        product = public_product(doc_id, data)
        # Rows can contain the same food in PC/ST units: offer the food only once.
        identity = normalize(product['codigo'] or product['nome'])
        value = sum(occasions.get(v, 0) for v in product['ocasioes']) + sum(types.get(v, 0) for v in product['tiposProduto'])
        if 'carne moida' in text and 'moida' in normalize(product['nome']): value += 12
        if not any(t in types for t in product['tiposProduto']): value -= 8
        tie = hashlib.sha256((context['cliente']['id'] + doc_id).encode()).hexdigest()
        candidate = (value, bool(product['imagem_url']), tie, product)
        if identity not in unique or candidate[:3] > unique[identity][:3]: unique[identity] = candidate
    ranked = sorted(unique.values(), key=lambda candidate: candidate[:3], reverse=True)
    chosen, names = [], set()
    for value, photo, tie, product in ranked:
        # Also avoid duplicate descriptions recorded under different internal codes.
        name = normalize(product['nome'])
        if name in names: continue
        names.add(name); chosen.append(product)
        if len(chosen) == 3: break
    if len(chosen) < 3:
        from services import ServiceError
        raise ServiceError('INSUFFICIENT_PRODUCTS',503)
    # Do not expose a retrieval rank or place the highest ranked food in a fixed slot.
    seed = repr(context['conversa'])
    chosen.sort(key=lambda p: hashlib.sha256((seed+p['id']).encode()).hexdigest())
    return dict(cliente_id=context['cliente']['id'], no_atual=context['conversa'][-1]['id'],
                produtos=chosen, ficha_escuta=list(context['ficha_escuta'].values()),
                orientacao='Compare as fichas com as necessidades do cliente. Confirme composição e restrições antes de recomendar.')
