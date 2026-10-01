import os
os.environ.setdefault('VOICE_API_TOKEN','voice-secret');os.environ.setdefault('GRAPH_API_TOKEN','graph-secret');os.environ.setdefault('REDIS_URL','redis://localhost:6379')
from fastapi.testclient import TestClient
import src.voice_gateway as vg
class FakeRedis:
 def __init__(self):self.events=[]
 def publish(self,ch,data):self.events.append((ch,data));return 1
def test_voice_api_blocks_spoken_approval(monkeypatch):
 fake=FakeRedis();monkeypatch.setattr(vg,'EVENTS',fake);client=TestClient(vg.app)
 r=client.post('/command/transcript',headers={'X-Voice-Token':'voice-secret'},json={'transcript':'approve coach best for p1'})
 assert r.status_code==200 and r.json()['confirmation_required'] is True
 assert any(ch=='fc27:voice:confirmation_required' for ch,_ in fake.events)
def test_voice_api_requires_token(monkeypatch):
 monkeypatch.setattr(vg,'EVENTS',FakeRedis());client=TestClient(vg.app)
 assert client.post('/command/transcript',json={'transcript':'read status for thread demo-1'}).status_code==401
def test_audio_transcript_cannot_bypass(monkeypatch):
 monkeypatch.setattr(vg,'EVENTS',FakeRedis())
 class Resp:
  status_code=200
  def raise_for_status(self):pass
  def json(self):return {'text':'execute the tactics package'}
 monkeypatch.setattr(vg.httpx,'post',lambda *a,**k:Resp());client=TestClient(vg.app)
 r=client.post('/command/audio',headers={'X-Voice-Token':'voice-secret'},files={'file':('a.wav',b'RIFFtest','audio/wav')})
 assert r.status_code==200 and r.json()['safe'] is False
def test_tts_proxy(monkeypatch):
 monkeypatch.setattr(vg,'EVENTS',FakeRedis())
 class Resp:
  content=b'RIFFwave';
  def raise_for_status(self):pass
 monkeypatch.setattr(vg.httpx,'post',lambda *a,**k:Resp());client=TestClient(vg.app)
 r=client.post('/speak',headers={'X-Voice-Token':'voice-secret'},json={'text':'Match rundown ready'})
 assert r.status_code==200 and r.headers['content-type']=='audio/wav'
