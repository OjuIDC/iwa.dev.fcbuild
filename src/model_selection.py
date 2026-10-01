from __future__ import annotations
import json, os, urllib.request
from dataclasses import dataclass
from pathlib import Path
from .settings import env_bool
ROLES=("player","scout","analyst","coach","tactics","director","reviewer")
@dataclass(frozen=True)
class Route:
    role:str; provider:str; model:str; base_url:str; score:float; reason:str
class ModelRegistry:
    def __init__(self,catalog_path=None,matrix_path=None,results_path=None):
        root=Path(__file__).resolve().parents[1]
        self.catalog=json.loads(Path(catalog_path or os.getenv('MODEL_CATALOG',root/'config/model_catalog.json')).read_text())
        self.matrix=json.loads(Path(matrix_path or os.getenv('MODEL_BENCHMARK_MATRIX',root/'config/role_benchmark_matrix.json')).read_text())
        self.results={}
        rp=results_path or os.getenv('MODEL_BENCHMARK_RESULTS')
        if rp and Path(rp).exists(): self.load_results(rp)
        # LM Studio is used only when explicitly enabled. Invalid values raise ConfigError.
        self.lm_studio_enabled=env_bool('LM_STUDIO_FALLBACK_ENABLED',False)
        lm_url=os.getenv('LM_STUDIO_BASE_URL')
        if lm_url and 'lm_studio' in self.catalog.get('providers',{}): self.catalog['providers']['lm_studio']['base_url']=lm_url
    def load_results(self,path): self.results=json.loads(Path(path).read_text());return self
    def provider_enabled(self,provider):
        return provider!='lm_studio' or self.lm_studio_enabled
    def candidates(self,role):
        if role not in ROLES: raise ValueError(f"unsupported role: {role}")
        return [m for m in self.catalog['models'] if m.get('enabled',True) and role in m['roles'] and self.provider_enabled(m['provider'])]
    def role_result(self,m,role):
        return self.results.get('models',{}).get(f"{m['provider']}::{m['name']}",{}).get('roles',{}).get(role,{})
    def healthy(self,m,role):
        r=self.results.get('models',{}).get(f"{m['provider']}::{m['name']}",{}); rr=self.role_result(m,role); p=self.matrix['selection_policy']
        return r.get('available',False) and rr.get('schema',0)>=p['minimum_schema'] and rr.get('gate',0)>=p['minimum_gate'] and r.get('latency_ms',10**9)<=p['maximum_latency_ms'] and rr.get('total',0)>=p['minimum_total']
    def select(self,role):
        forced=os.getenv(f"{role.upper()}_MODEL"); items=[]
        for m in self.candidates(role):
            score=float(self.role_result(m,role).get('total',0)); healthy=self.healthy(m,role)
            # Healthy routes always outrank unhealthy routes. Forced only chooses among equally healthy routes.
            items.append((0 if healthy else 1,0 if forced and m['name']==forced else 1,-score,m['fallback_rank'],m))
        if not items: raise RuntimeError(f"no enabled model configured for {role}")
        items.sort(key=lambda x:x[:4]); m=items[0][4]; score=float(self.role_result(m,role).get('total',0)); p=self.catalog['providers'][m['provider']]
        reason='forced-healthy' if forced==m['name'] and self.healthy(m,role) else ('benchmark' if self.healthy(m,role) else 'configured-default-no-live-benchmark')
        return Route(role,m['provider'],m['name'],p['base_url'],score,reason)
    def fallback_chain(self,role):
        primary=self.select(role); chain=[primary]
        rest=[]
        for m in self.candidates(role):
            if (m['provider'],m['name'])==(primary.provider,primary.model): continue
            p=self.catalog['providers'][m['provider']]; score=float(self.role_result(m,role).get('total',0))
            rest.append((0 if self.healthy(m,role) else 1,-score,m['fallback_rank'],Route(role,m['provider'],m['name'],p['base_url'],score,'fallback')))
        chain.extend(x[-1] for x in sorted(rest,key=lambda x:x[:3]));return chain
class OpenAICompatibleClient:
    def __init__(self,timeout=None):self.timeout=float(timeout if timeout is not None else os.getenv('ROLE_TIMEOUT_SEC','120'))
    def invoke(self,route,messages):
        base=route.base_url.rstrip('/')
        if route.provider=='ollama': endpoint=base+'/api/chat';payload={'model':route.model,'messages':messages,'stream':False}
        else: endpoint=base+'/chat/completions';payload={'model':route.model,'messages':messages,'stream':False}
        req=urllib.request.Request(endpoint,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer local-only'})
        with urllib.request.urlopen(req,timeout=self.timeout) as resp:data=json.loads(resp.read())
        return data.get('message',{}).get('content') or data.get('choices',[{}])[0].get('message',{}).get('content','')
def invoke_role(role,messages,registry=None,client=None):
    registry=registry or ModelRegistry();client=client or OpenAICompatibleClient();errors=[]
    for route in registry.fallback_chain(role):
        if not registry.provider_enabled(route.provider):  # defence in depth
            continue
        try:
            text=client.invoke(route,messages)
            if not text:raise RuntimeError('empty model response')
            return {'content':text,'route':route,'fallback_used':bool(errors),'errors':errors}
        except Exception as exc:errors.append({'provider':route.provider,'model':route.model,'error':str(exc)})
    raise RuntimeError(f"all model routes failed for {role}: {errors}")
