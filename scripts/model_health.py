from __future__ import annotations
import argparse, json, os, time, urllib.request
BASE=os.getenv("OLLAMA_URL","http://ollama:11434").rstrip("/")
REQUIRED=os.getenv("REQUIRED_MODELS","llama3.2:3b qwen2.5:7b").split()
def get(path):
    with urllib.request.urlopen(BASE+path,timeout=10) as r:return json.loads(r.read())
def post(path,payload):
    req=urllib.request.Request(BASE+path,data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=120) as r:return json.loads(r.read())
def check():
    tags=get('/api/tags'); installed={m['name'] for m in tags.get('models',[])}
    missing=[m for m in REQUIRED if m not in installed and not any(x.startswith(m+'-') for x in installed)]
    if missing: raise RuntimeError(f"missing models: {missing}")
    for model in REQUIRED:
        result=post('/api/generate',{"model":model,"prompt":"Return exactly HEALTHY","stream":False,"options":{"num_predict":4,"temperature":0}})
        if not result.get('response'): raise RuntimeError(f"empty generation: {model}")
    open('/tmp/models-healthy','w').write(str(time.time()))
    return {"status":"healthy","required":REQUIRED,"installed":sorted(installed)}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--watch',action='store_true');a=p.parse_args()
    while True:
        try: print(json.dumps(check()),flush=True)
        except Exception as e:
            try: os.remove('/tmp/models-healthy')
            except FileNotFoundError: pass
            print(json.dumps({"status":"unhealthy","error":str(e)}),flush=True)
            if not a.watch: raise
        if not a.watch: break
        time.sleep(30)
