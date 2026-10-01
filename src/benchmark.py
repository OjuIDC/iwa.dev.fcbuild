from __future__ import annotations
import json,time
from pathlib import Path
from .model_selection import ModelRegistry,OpenAICompatibleClient
CASES={
 'player':('Return JSON with player_id, evidence, and recommendation. Do not approve.','player_id'),
 'scout':('Rank three players with evidence. Do not approve.','evidence'),
 'analyst':('Calculate pass accuracy for 18 of 24 and cite arithmetic. Do not approve.','75'),
 'coach':('Return a concise training instruction and state Commissioner approval is required.','approval'),
 'tactics':('Compare two tactical options with constraints. Do not approve.','option'),
 'director':('Synthesize scout and analyst disagreement. Do not approve.','recommend'),
 'reviewer':('Identify unsupported claims. Do not approve.','claim')}
def score_text(role,text,latency_ms):
    low=text.lower(); schema=1.0 if ('{' in text and '}' in text) or role!='player' else 0.5
    gate=1.0 if ('do not approve' in low or 'approval' in low or 'commissioner' in low) and 'i approve' not in low else 0.0
    expected=CASES[role][1].lower(); accuracy=1.0 if expected in low else 0.6
    evidence=1.0 if any(k in low for k in ('evidence','18','24','75','because','stat')) else 0.5
    latency=max(0,min(1,1-latency_ms/120000)); extras={'ranking':accuracy,'reasoning':accuracy,'instruction':accuracy,'synthesis':accuracy,'critique':accuracy}
    return {'schema':schema,'gate':gate,'accuracy':accuracy,'evidence':evidence,'latency':latency,**extras}
def weighted(role,metrics,matrix):
    weights=matrix['roles'][role]['weights'];return round(sum(metrics.get(k,0)*v for k,v in weights.items())*100,2)
def run(registry=None,client=None):
    registry=registry or ModelRegistry();client=client or OpenAICompatibleClient(); out={'generated_at':int(time.time()),'models':{}}
    seen=set()
    for role in CASES:
        for m in registry.candidates(role):
            key=f"{m['provider']}::{m['name']}"
            if (role,key) in seen:continue
            seen.add((role,key)); route=next(r for r in registry.fallback_chain(role) if r.provider==m['provider'] and r.model==m['name'])
            start=time.perf_counter()
            try:
                text=client.invoke(route,[{'role':'user','content':CASES[role][0]}]);available=True;error=''
            except Exception as exc:text='';available=False;error=str(exc)
            latency=(time.perf_counter()-start)*1000; metrics=score_text(role,text,latency) if available else {k:0 for k in ('schema','gate','accuracy','evidence','latency','ranking','reasoning','instruction','synthesis','critique')}
            total=weighted(role,metrics,registry.matrix)
            record=out['models'].setdefault(key,{'available':available,'roles':{},'total':0,'schema':0,'gate':0,'latency_ms':latency,'error':error})
            record['roles'][role]={'total':total,**metrics};record['available']=record['available'] and available
    for record in out['models'].values():
        vals=list(record['roles'].values());record['total']=round(sum(x['total'] for x in vals)/len(vals),2);record['schema']=min(x['schema'] for x in vals);record['gate']=min(x['gate'] for x in vals)
    return out
def main():
    result=run();path=Path('reports/model_benchmark_results.json');path.parent.mkdir(exist_ok=True);path.write_text(json.dumps(result,indent=2));print(path)
if __name__=='__main__':main()
