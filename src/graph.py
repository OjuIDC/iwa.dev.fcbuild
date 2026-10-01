from __future__ import annotations
import json, os, time
from typing import Any
import httpx
from .model_selection import ModelRegistry, invoke_role
from .lead_packages import build_lead_packages
CLUB_ID=os.getenv("FC27_CLUB_ID","330323"); PLATFORM=os.getenv("FC_PLATFORM","common-gen5"); MCP=os.getenv("FC27_MCP_URL","http://fc27-mcp:8000")
def now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
def mcp_call(tool,args):
 r=httpx.post(MCP,json={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":tool,"arguments":args}},timeout=30);r.raise_for_status();d=r.json()
 if "error" in d: raise RuntimeError(d["error"]["message"])
 return json.loads(d["result"]["content"][0]["text"])
def ask(role,system,data,registry=None):
 return invoke_role(role,[{"role":"system","content":system},{"role":"user","content":json.dumps(data) }],registry or ModelRegistry())["content"]
def initial_state(thread_id,club_id=CLUB_ID,match=None):
 if not thread_id: raise ValueError("thread_id is allocated by the Graph API")
 return {"thread_id":thread_id,"club_id":club_id,"platform":PLATFORM,"fc_version":"27","match":match,"status":"running","approved":None,"audit_log":[],"errors":[]}
def run_pipeline(state:dict, registry=None, dashboard=None):
 reg=registry or ModelRegistry(); state=dict(state); state["status"]="running"
 try:
  dashboard=dashboard or mcp_call("get_club_dashboard",{"club_id":state["club_id"],"platform":state["platform"]})
  state["dashboard"]=dashboard; state["audit_log"].append({"node":"search","ts":now(),"summary":"dashboard loaded"})
  scout=ask("scout","Return evidence-grounded scouting recommendations. Never approve actions.",dashboard,reg)
  analyst=ask("analyst","Analyze numeric performance. Never approve actions.",{"dashboard":dashboard,"scout":scout},reg)
  coach=ask("coach","Create an advisory training plan requiring Commissioner approval.",{"analyst":analyst},reg)
  tactics=ask("tactics","Compare tactical options. Never deploy or approve.",{"dashboard":dashboard,"analyst":analyst},reg)
  director=ask("director","Synthesize reports into an advisory recommendation requiring Commissioner approval.",{"scout":scout,"analyst":analyst,"coach":coach,"tactics":tactics},reg)
  reports={"scout":scout,"analyst":analyst,"coach":coach,"tactics":tactics,"director":director}
  packages=build_lead_packages(dashboard,state["club_id"],reports)
  state.update(reports=reports,lead_packages=packages.model_dump(mode="json"),status="awaiting_approval")
  state["audit_log"].append({"node":"recommendation","ts":now(),"summary":"reports complete; Commissioner approval required"})
 except Exception as exc: state["status"]="failed";state["errors"].append(str(exc))
 return state
# Decisions are applied only by src.approval_store (signed, nonce-bound, atomic).
