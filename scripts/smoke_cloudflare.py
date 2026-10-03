"""Owner-only manual test using an EXISTING Firebase user's temporary session.

Optional tail output is restricted to sanitized diagnostics emitted by our code.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from services import post_or_get,b64
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import padding

def filtered_tail(process):
    buffer=''
    decoder=json.JSONDecoder()
    for line in process.stdout:
        buffer+=line
        while True:
            start=buffer.find('{')
            if start<0:
                buffer='';break
            buffer=buffer[start:]
            try:
                event,end=decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                break
            buffer=buffer[end:]
            for log in event.get('logs',[]):
                for message in log.get('message',[]):
                    try: diagnostic=json.loads(message)
                    except (TypeError,ValueError): continue
                    if diagnostic.get('event')=='evaluation_failed':
                        print({k:diagnostic.get(k) for k in ['event','stage','reason','markers','frames']},flush=True)

async def run(args):
    base=args.service_url.rstrip('/')
    account=json.loads(Path(args.firebase_file).read_text(encoding='utf-8-sig'))
    now=int(time.time())
    claims=dict(iss=account['client_email'],sub=account['client_email'],
                aud='https://identitytoolkit.googleapis.com/google.identity.identitytoolkit.v1.IdentityToolkit',
                iat=now,exp=now+300,uid=args.existing_uid)
    message=(b64(b'{"alg":"RS256","typ":"JWT"}')+'.'+b64(json.dumps(claims).encode())).encode()
    key=serialization.load_pem_private_key(account['private_key'].encode(),None)
    token=message.decode()+'.'+b64(key.sign(message,padding.PKCS1v15(),hashes.SHA256()))
    response=await post_or_get('https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key='+args.web_key,
                               payload={'token':token,'returnSecureToken':True})
    if response.status!=200: raise RuntimeError('Authentication test failed')
    id_token=response.json()['idToken']
    payload=dict(cliente_id='cliente1',no_atual='d3',produto_id=args.product,
                 historico=[dict(no_id='d1',opcao_id='d1-o1'),dict(no_id='d2',opcao_id='d2-o1')],
                 quantidade=dict(unidades=3,peso_total_kg=2.1))
    anonymous=await post_or_get(base+'/v1/avaliacoes',payload=payload)
    print('anonymous_status',anonymous.status,flush=True)
    response=await post_or_get(base+'/v1/avaliacoes',payload=payload,headers={'Authorization':'Bearer '+id_token})
    output=response.json()
    print(dict(status=response.status,score=output.get('score'),classificacao=output.get('classificacao'),detail=output.get('detail')),flush=True)
    return response.status==200 and anonymous.status==401

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--firebase-file',required=True)
    parser.add_argument('--existing-uid',required=True)
    parser.add_argument('--web-key',required=True)
    parser.add_argument('--product',required=True)
    parser.add_argument('--service-url',default='https://epav-product-evaluator.kevinernandes2012.workers.dev')
    parser.add_argument('--tail',action='store_true')
    args=parser.parse_args()
    process=None
    try:
        if args.tail:
            os.environ['CLOUDFLARE_ACCOUNT_ID']='9cc0381fc2388fed8647d4ce2c6c7d9c'
            process=subprocess.Popen(['node','node_modules/wrangler/bin/wrangler.js','tail','--format','json','--profile','epav'],
                                     stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8')
            threading.Thread(target=filtered_tail,args=(process,),daemon=True).start()
            time.sleep(5)
        success=asyncio.run(run(args))
        if process: time.sleep(3)
    finally:
        if process: process.terminate()
    if not success: raise SystemExit(1)

if __name__=='__main__':main()
