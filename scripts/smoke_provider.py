"""Explicit manual smoke test. Never prints keys, tokens or private catalog fields."""
import argparse
import asyncio
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from services import GroqPool,FirebaseService,ServiceError
from models import EvaluationRequest
from context import build_context
from evaluator import provider_payload,final_result

async def run(args):
    keys=Path(args.groq_file).read_text(encoding='utf-8-sig')
    account=Path(args.firebase_file).read_text(encoding='utf-8-sig')
    firebase=FirebaseService('',account)
    pool=GroqPool(keys)
    for doc_id in args.product:
        choice=EvaluationRequest(cliente_id='cliente1',no_atual='d3',produto_id=doc_id,
                                 historico=[dict(no_id='d1',opcao_id='d1-o1'),dict(no_id='d2',opcao_id='d2-o1')],
                                 quantidade=dict(unidades=3,peso_total_kg=2.1))
        product=await firebase.product(doc_id)
        context=build_context(choice,product)
        answer=await pool.complete(provider_payload(context,'openai/gpt-oss-20b'))
        output=final_result(answer,context,'openai/gpt-oss-20b')
        print(dict(produto=product['nome'],score=output.score,classificacao=output.classificacao,resumo=output.resumo),flush=True)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--groq-file',required=True)
    parser.add_argument('--firebase-file',required=True)
    parser.add_argument('--product',action='append',required=True)
    args=parser.parse_args()
    try:
        asyncio.run(run(args))
    except ServiceError as error:
        print('SMOKE_FAILED '+error.code)
        raise SystemExit(1) from None

if __name__=='__main__': main()
