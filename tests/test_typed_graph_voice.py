import os
os.environ.setdefault('VOICE_API_TOKEN','voice-secret');os.environ.setdefault('GRAPH_SERVICE_TOKEN','graph-service-secret')
from src.voice_gateway import parse_command
def test_voice_approval_is_never_executed():
 result=parse_command('approve the coach best recommendation now')
 assert result['safe'] is False and result['confirmation_required'] is True and result['intent']=='approval_attempt'
def test_voice_reject_and_execute_are_blocked():
 for phrase in ('reject thread abc','execute the tactics package','activate training'):
  assert parse_command(phrase)['safe'] is False
def test_voice_read_only_thread_command():
 result=parse_command('read status for thread demo-123')
 assert result['safe'] is True and result['intent']=='read_thread' and result['thread_id']=='demo-123'
