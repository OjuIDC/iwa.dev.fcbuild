import os,subprocess,tempfile
from fastapi import FastAPI,HTTPException
from fastapi.responses import Response
from pydantic import BaseModel,Field
app=FastAPI(title="Local TTS")
class Speak(BaseModel):text:str=Field(min_length=1,max_length=4000);voice:str='default'
@app.get('/health')
def health():return {'status':'ok','engine':'espeak-ng'}
@app.post('/synthesize')
def synthesize(body:Speak):
 with tempfile.NamedTemporaryFile(suffix='.wav') as f:
  cmd=['espeak-ng','-w',f.name,'-s',os.getenv('TTS_SPEED','165'),body.text]
  result=subprocess.run(cmd,capture_output=True,timeout=30)
  if result.returncode:raise HTTPException(500,result.stderr.decode(errors='ignore'))
  f.seek(0);return Response(f.read(),media_type='audio/wav')
