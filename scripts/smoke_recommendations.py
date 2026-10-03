"""Manual deployment check with an existing owner's temporary Firebase session."""
import asyncio, argparse, json, sys, time
import httpx
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from services import post_or_get, b64
from recommendations import STAGES
from scenarios import SCENARIOS
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import padding

async def api_request(url,payload,headers):
    async with httpx.AsyncClient(timeout=55,follow_redirects=False) as client:
        response=await client.post(url,json=payload,headers=headers)
        from services import HTTPResult
        return HTTPResult(response.status_code,{},response.text)

async def run(args):
    account=json.loads(Path(args.firebase_file).read_text(encoding='utf-8-sig'))
    now=int(time.time())
    claims=dict(iss=account['client_email'],sub=account['client_email'],aud='https://identitytoolkit.googleapis.com/google.identity.identitytoolkit.v1.IdentityToolkit',iat=now,exp=now+300,uid=args.existing_uid)
    message=(b64(b'{"alg":"RS256","typ":"JWT"}')+'.'+b64(json.dumps(claims).encode())).encode()
    key=serialization.load_pem_private_key(account['private_key'].encode(),None)
    token=message.decode()+'.'+b64(key.sign(message,padding.PKCS1v15(),hashes.SHA256()))
    response=await post_or_get('https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key='+args.web_key,payload={'token':token,'returnSecureToken':True})
    if response.status!=200: raise RuntimeError('Authentication check failed')
    headers={'Authorization':'Bearer '+response.json()['idToken']}
    for client_id, stage in STAGES.items():
        history=[]
        for node in SCENARIOS[client_id]['dialogo']:
            if node==stage: break
            history.append({'no_id':node,'opcao_id':node+'-o1'})
        choice=dict(cliente_id=client_id,no_atual=stage,historico=history)
        response=await api_request(args.service_url+'/v1/recomendacoes',payload=choice,headers=headers)
        try: result=response.json()
        except ValueError: result={}
        products=result.get('produtos',[])
        print(dict(cliente=client_id,status=response.status,produtos=len(products),detail=result.get('detail')),flush=True)
        if response.status!=200 or len({p['codigo'] for p in products})!=3: raise RuntimeError('Recommendation check failed')
        if client_id=='cliente1' and args.evaluate:
            result=await api_request(args.service_url+'/v1/avaliacoes',payload=dict(choice,produto_id=products[0]['id'],quantidade={'unidades':3}),headers=headers)
            output=result.json()
            print(dict(avaliacao_status=result.status,score=output.get('score'),detail=output.get('detail')),flush=True)
            if result.status!=200: raise RuntimeError('Evaluation check failed')

parser=argparse.ArgumentParser()
parser.add_argument('--firebase-file',required=True)
parser.add_argument('--existing-uid',required=True)
parser.add_argument('--web-key',required=True)
parser.add_argument('--service-url',default='https://epav-product-evaluator.kevinernandes2012.workers.dev')
parser.add_argument('--evaluate',action='store_true')
asyncio.run(run(parser.parse_args()))
