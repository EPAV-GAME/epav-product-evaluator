"""Use after the owner authorizes saving credentials to this specific Worker."""
import argparse
import json
import os
import subprocess
from pathlib import Path

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--groq-file',required=True)
    parser.add_argument('--firebase-file',required=True)
    args=parser.parse_args()
    raw=Path(args.groq_file).read_text(encoding='utf-8-sig').strip()
    # Validate without printing credentials, even on errors.
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
    from services import parse_keys
    keys=parse_keys(raw)
    account=Path(args.firebase_file).read_text(encoding='utf-8-sig')
    if json.loads(account).get('project_id')!='epav-game':
        raise RuntimeError('Expected epav-game project')
    os.environ['CLOUDFLARE_ACCOUNT_ID']='9cc0381fc2388fed8647d4ce2c6c7d9c'
    for name,payload in [('GROQ_API_KEYS','|'.join(keys)),('FIREBASE_SERVICE_ACCOUNT_JSON',account)]:
        result=subprocess.run(['node','node_modules/wrangler/bin/wrangler.js','secret','put',name,'--profile','epav'],
                              input=payload.encode(),capture_output=True)
        if result.returncode:
            raise RuntimeError('Configuration failed for '+name)
        print(name+' configured.',flush=True)

if __name__=='__main__': main()
