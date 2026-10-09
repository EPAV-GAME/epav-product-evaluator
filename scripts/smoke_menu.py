"""Read-only production verification using an existing owner's temporary session."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
import httpx
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import padding
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from services import b64
from menu import CATEGORIES,MENU_STAGES
from scenarios import SCENARIOS

def payload(category,roteiro='legado',popup_file=None):
    if roteiro=='popup-v1':
        script=json.loads(Path(popup_file).read_text(encoding='utf-8-sig'))['clientes']['lucas']
        stage=next(s for s in script['etapas'] if s.get('popup_produtos_ref')==category)
        history=[]
        for s in script['etapas']:
            if s['id']==stage['id']:break
            history.append(dict(no_id=s['id'],opcao_id=s['alternativas_cadastradas'][0]['id']))
        return dict(roteiro=roteiro,cliente_id='cliente1',no_atual=stage['id'],categoria=category,historico=history)
    from scenarios_v2 import SCENARIOS_V2
    scripts=SCENARIOS_V2 if roteiro=='ia-v2' else SCENARIOS
    stage=MENU_STAGES['cliente1'][category];history=[];node='d1'
    while node!=stage:
        option=scripts['cliente1']['dialogo'][node]['opcoes'][0]
        history.append(dict(no_id=node,opcao_id=option['id']));node=option['proximoNo']
    return dict(cliente_id='cliente1',no_atual=stage,categoria=category,historico=history,**({'roteiro':roteiro} if roteiro!='legado' else {}))

async def run(args):
    if args.roteiro=='popup-v1' and (not args.popup_file or args.evaluate):
        raise ValueError('popup-v1 requires --popup-file and local scoring; do not use --evaluate.')
    async with httpx.AsyncClient(timeout=55,follow_redirects=False) as client:
        if args.firebase_file:
            raw=Path(args.firebase_file).read_text(encoding='utf-8-sig');account=json.loads(raw)
            now=int(time.time())
            key=serialization.load_pem_private_key(account['private_key'].encode(),None)
            oauth_claims=dict(iss=account['client_email'],scope='https://www.googleapis.com/auth/cloud-platform',
                aud='https://oauth2.googleapis.com/token',iat=now,exp=now+300)
            oauth_message=(b64(b'{"alg":"RS256","typ":"JWT"}')+'.'+b64(json.dumps(oauth_claims).encode())).encode()
            assertion=oauth_message.decode()+'.'+b64(key.sign(oauth_message,padding.PKCS1v15(),hashes.SHA256()))
            oauth=await client.post('https://oauth2.googleapis.com/token',data={'grant_type':'urn:ietf:params:oauth:grant-type:jwt-bearer','assertion':assertion})
            oauth.raise_for_status()
            lookup=await client.post('https://identitytoolkit.googleapis.com/v1/projects/epav-game/accounts:lookup',
                headers={'Authorization':'Bearer '+oauth.json()['access_token']},json={'email':[args.existing_email]})
            lookup.raise_for_status();users=lookup.json().get('users',[])
            if len(users)!=1:raise RuntimeError('An existing account is required; no user is created.')
            now=int(time.time());claims=dict(iss=account['client_email'],sub=account['client_email'],
                aud='https://identitytoolkit.googleapis.com/google.identity.identitytoolkit.v1.IdentityToolkit',iat=now,exp=now+300,uid=users[0]['localId'])
            message=(b64(b'{"alg":"RS256","typ":"JWT"}')+'.'+b64(json.dumps(claims).encode())).encode()
            key=serialization.load_pem_private_key(account['private_key'].encode(),None)
            custom=message.decode()+'.'+b64(key.sign(message,padding.PKCS1v15(),hashes.SHA256()))
            login=await client.post('https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key='+args.web_key,
                json={'token':custom,'returnSecureToken':True})
        else:
            from getpass import getpass
            password=getpass('Senha da conta existente (não será salva): ')
            login=await client.post('https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key='+args.web_key,
                json={'email':args.existing_email,'password':password,'returnSecureToken':True})
            del password
        login.raise_for_status();headers={'Authorization':'Bearer '+login.json()['idToken']}
        options={}
        for category in CATEGORIES:
            started=time.monotonic()
            endpoint='/v2/recomendacoes' if args.roteiro=='popup-v1' else '/v1/recomendacoes'
            response=await client.post(args.service_url+endpoint,headers=headers,json=payload(category,args.roteiro,args.popup_file))
            if not response.headers.get('content-type','').startswith('application/json'):
                print(json.dumps(dict(categoria=category,status=response.status_code,
                    formato=response.headers.get('content-type'),segundos=round(time.monotonic()-started,3))),flush=True)
                raise RuntimeError('The Worker returned a non-JSON response')
            data=response.json();products=data.get('produtos',[])
            print(json.dumps(dict(categoria=category,status=response.status_code,opcoes=len(products),
                disponiveis=data.get('total_disponiveis'),segundos=round(time.monotonic()-started,3),detail=data.get('detail')),ensure_ascii=False),flush=True)
            if response.status_code!=200 or len(products)!=args.expected_options or len({p['id'] for p in products})!=args.expected_options:
                raise RuntimeError('Menu availability check failed')
            options[category]=products
        semaphore=asyncio.Semaphore(6)
        async def check_image(product):
            async with semaphore:
                response=await client.head(product['imagem_url'])
                return response.status_code==200 and response.headers.get('content-type','').startswith('image/webp')
        photos=[p for products in options.values() for p in products if p.get('imagem_url')]
        images=await asyncio.gather(*(check_image(p) for p in photos))
        print(json.dumps(dict(fotos_verificadas=len(images),fotos_validas=sum(images))),flush=True)
        if args.roteiro=='popup-v1':print(json.dumps(dict(opcoes_sem_foto=sum(len(p) for p in options.values())-len(photos))),flush=True)
        if not all(images):raise RuntimeError('Some recommended photos are unavailable')
        if args.cache_status:
            response=await client.get('https://epav-product-evaluator.kevinernandes2012.workers.dev/v1/cache/status',headers=headers)
            print(json.dumps(dict(cache_status=response.status_code,cache=response.json())),flush=True)
            if response.status_code!=200 or not response.json().get('redis_disponivel'):raise RuntimeError('Shared Redis verification failed')
        if args.evaluate:
            previous=[]
            for category in CATEGORIES if args.evaluate_all else ['entrada','principal']:
                product=options[category][0];quantity={'unidades':2}
                data=dict(payload(category,args.roteiro),produto_id=product['id'],quantidade=quantity,escolhas_anteriores=previous)
                started=time.monotonic();response=await client.post(args.service_url+'/v1/avaliacoes',headers=headers,json=data)
                answer=response.json()
                print(json.dumps(dict(avaliacao=category,status=response.status_code,score=answer.get('score'),
                    escolhas_anteriores=len(previous),segundos=round(time.monotonic()-started,3),timing=response.headers.get('server-timing'),detail=answer.get('detail')),ensure_ascii=False),flush=True)
                if response.status_code!=200 or not isinstance(answer.get('score'),int):raise RuntimeError('Menu assessment failed')
                previous.append(dict(categoria=category,produto_id=product['id'],quantidade=quantity))

parser=argparse.ArgumentParser()
parser.add_argument('--firebase-file')
parser.add_argument('--existing-email',required=True)
parser.add_argument('--web-key',required=True)
parser.add_argument('--service-url',default='https://epav-product-evaluator.kevinernandes2012.workers.dev')
parser.add_argument('--evaluate',action='store_true')
parser.add_argument('--evaluate-all',action='store_true')
parser.add_argument('--cache-status',action='store_true')
parser.add_argument('--expected-options',type=int,choices=[5,10],default=10)
parser.add_argument('--roteiro',choices=['legado','ia-v2','popup-v1'],default='legado')
parser.add_argument('--popup-file')
asyncio.run(run(parser.parse_args()))
