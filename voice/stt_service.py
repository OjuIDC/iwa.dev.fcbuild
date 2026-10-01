import os,tempfile
from fastapi import FastAPI,File,UploadFile,HTTPException
from faster_whisper import WhisperModel
app=FastAPI(title="Local STT")
model=None
def get_model():
 global model
 if model is None:model=WhisperModel(os.getenv("STT_MODEL","small.en"),device=os.getenv("STT_DEVICE","cpu"),compute_type=os.getenv("STT_COMPUTE_TYPE","int8"))
 return model
@app.get('/health')
def health():
 get_model();return {'status':'ok','model':os.getenv('STT_MODEL','small.en')}
@app.post('/transcribe')
async def transcribe(file:UploadFile=File(...)):
 data=await file.read()
 if not data or len(data)>10*1024*1024:raise HTTPException(400,'invalid audio size')
 suffix=os.path.splitext(file.filename or '.wav')[1] or '.wav'
 with tempfile.NamedTemporaryFile(suffix=suffix) as f:
  f.write(data);f.flush();segments,_=get_model().transcribe(f.name,vad_filter=True,beam_size=1)
  text=' '.join(x.text.strip() for x in segments).strip()
 return {'text':text}
