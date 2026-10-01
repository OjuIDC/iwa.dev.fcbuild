from __future__ import annotations
import hashlib,hmac,json,os,re,time
import httpx
import redis
from fastapi import FastAPI,File,Header,HTTPException,UploadFile
from fastapi.responses import Response
from pydantic import BaseModel,Field
app=FastAPI(title="FC27 Voice Gateway")
TOKEN=os.environ["VOICE_API_TOKEN"];GRAPH=os.getenv("GRAPH_URL","http://graph-api:8001");GRAPH_TOKEN=os.environ["GRAPH_SERVICE_TOKEN"]  # read-only service role; cannot approve
STT=os.getenv("STT_URL","http://stt:9001");TTS=os.getenv("TTS_URL","http://tts:9002")
EVENTS=redis.Redis.from_url(os.getenv("REDIS_URL","redis://redis:6379"),decode_responses=True)
def auth(value):
 if not value or not hmac.compare_digest(value.encode(),TOKEN.encode()):raise HTTPException(401,"invalid voice token")
class Transcript(BaseModel): transcript:str=Field(min_length=1,max_length=2000)
class Speak(BaseModel): text:str=Field(min_length=1,max_length=4000);voice:str="default"
def parse_command(text):
 t=" ".join(text.lower().split())
 if re.search(r"\b(approve|reject|authorize|execute|deploy|activate)\b",t):
  return {"intent":"approval_attempt","safe":False,"confirmation_required":True,"message":"Voice cannot approve or reject. Open the authenticated visual approval screen and confirm the exact typed lead-package selection."}
 m=re.search(r"(?:status|read|show)(?: status)?(?: for)? thread ([a-z0-9-]{3,80})",t)
 if m:return {"intent":"read_thread","safe":True,"thread_id":m.group(1)}
 return {"intent":"query","safe":True,"message":"Voice supports read-only thread status and spoken summaries. Approval remains visual and authenticated."}
@app.get('/health')
def health():return {'status':'ok','approval_by_voice':False}
@app.post('/command/transcript')
def transcript(body:Transcript,x_voice_token:str|None=Header(default=None)):
 auth(x_voice_token);cmd=parse_command(body.transcript)
 if cmd.get('intent')=='read_thread':
  r=httpx.get(f"{GRAPH}/threads/{cmd['thread_id']}",headers={'X-API-Key':GRAPH_TOKEN},timeout=20)
  if r.status_code==404:raise HTTPException(404,'thread not found')
  r.raise_for_status();state=r.json();cmd['result']={'thread_id':state['thread_id'],'status':state['status'],'lead_packages':state.get('lead_packages',{})}
 cmd['transcript_hash']=hashlib.sha256(body.transcript.encode()).hexdigest();cmd['ts']=int(time.time())
 EVENTS.publish('fc27:voice:transcript',json.dumps({k:v for k,v in cmd.items() if k!='result'}))
 if cmd.get('confirmation_required'):EVENTS.publish('fc27:voice:confirmation_required',json.dumps(cmd))
 return cmd
@app.post('/command/audio')
async def audio(file:UploadFile=File(...),x_voice_token:str|None=Header(default=None)):
 auth(x_voice_token);content=await file.read()
 if len(content)>10*1024*1024:raise HTTPException(413,'audio too large')
 r=httpx.post(f"{STT}/transcribe",files={'file':(file.filename or 'audio.wav',content,file.content_type or 'audio/wav')},timeout=120);r.raise_for_status()
 return transcript(Transcript(transcript=r.json()['text']),x_voice_token)
@app.post('/speak')
def speak(body:Speak,x_voice_token:str|None=Header(default=None)):
 auth(x_voice_token);r=httpx.post(f"{TTS}/synthesize",json=body.model_dump(),timeout=60);r.raise_for_status();return Response(r.content,media_type='audio/wav')
