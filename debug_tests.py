import os
os.environ['TEST_POSTGRES_URI'] = 'postgresql://fc27test@127.0.0.1:55432/postgres'
os.environ['PYTHONPATH'] = 'C:\\Users\\iwadev\\dev\\iwa.dev.fcbuild\\src'

from tests.helpers import make_app, settings, signed_cli_hmac, SVC, COM, StubPipeline

APPROVE_INTENT = {"decision": "APPROVE", "owner": "coach", "selection": "best",
                  "candidate_id": "coach-best-p1", "player_id": "p1", "notes": "approved candidate"}
import psycopg

with psycopg.connect('postgresql://fc27test@127.0.0.1:55432/postgres', autocommit=True) as conn:
    name = 'fc27_test_debug'
    conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    conn.execute(f'CREATE DATABASE "{name}"')

uri = f'postgresql://fc27test@127.0.0.1:55432/{name}'
from src.db_roles import bootstrap_database
bootstrap_database(uri, legacy_owner=None, confirm=True)

from src.db_migrate import migrate
info = psycopg.conninfo.conninfo_to_dict(uri)
info['user'] = 'fc27_migrator'
migrator = psycopg.conninfo.make_conninfo(**info)
migrate(migrator)

info['user'] = 'fc27_app'
app_uri = psycopg.conninfo.make_conninfo(**info)

# Create app with HMAC enabled
from src.graph_api import ApiSettings
from src.security import Authenticator
import hashlib
SERVICE_TOKEN = 'svc-test-token-0123456789abcdef0123456789abcdef'
COMMISSIONER_TOKEN = 'commissioner-test-token-0123456789abcdef01234567'
SIGNING_SECRET = 'signing-test-secret-0123456789abcdef0123456789ab'
COMMISSIONER_ID = 'batmanL8'

cli_hmac_settings = ApiSettings(
    postgres_uri=app_uri,
    authenticator=Authenticator(SERVICE_TOKEN, hashlib.sha256(COMMISSIONER_TOKEN.encode()).hexdigest(), COMMISSIONER_ID),
    signing_secret=SIGNING_SECRET,
    approval_ttl_seconds=120,
    redis_url=None,
    run_migrations=False,
    cli_hmac_enabled=True,
)
from src.graph import GraphDeps, GraphRunner
from src.roles import RoleOutput, RoleResult
from src.approval_store import ApprovalStore

class FakeRoles:
    def __init__(self, adjustments=None, invalid=()):
        self.adjustments = adjustments or {}
        self.invalid = set(invalid)
        self.calls = []
    def __call__(self, role, data, registry=None):
        self.calls.append(role)
        if role in self.invalid:
            return RoleResult(role=role, valid=False, error='invalid structured output: ValidationError', provider='ollama', model='m')
        assessments = [{'player_id': p, 'adjustment': a, 'reason': 'fixture'} for p, a in self.adjustments.get(role, {}).items()]
        return RoleResult(role=role, valid=True, provider='ollama', model='qwen2.5:7b',
                          output=RoleOutput(summary=f'{role} summary', assessments=assessments))

def dashboard(players=None):
    PLAYERS = {f'p{i}': {'rating': 9 - i * 0.4, 'goals': 0, 'assists': 0, 'passattempts': 10, 'passesmade': 8} for i in range(9)}
    return {'matches': [{'players': {'330323': players or PLAYERS}}], 'members': {}, 'overall': {}}

store = ApprovalStore(app_uri)

def mcp(tool, args):
    return dashboard()

runner = GraphRunner.from_conninfo(app_uri, GraphDeps(mcp=mcp, role_runner=FakeRoles(), registry_factory=lambda: None, decision_reader=store.load))
from src.graph_api import create_app
events = []
app = create_app(cli_hmac_settings, runner=runner, publisher=lambda c, p: events.append((c, p)))

from fastapi.testclient import TestClient
client = TestClient(app)

# Test 8: Modified owner
resp = client.post('/v1/runs', headers=SVC, json={'club_id': '330323'})
tid = resp.json()['thread_id']
ch = client.post(f'/v1/runs/{tid}/decision-challenge', headers=COM, json=APPROVE_INTENT).json()
print('Challenge:', ch)
ch_modified = {**ch, 'owner': 'scout'}
print('Modified:', ch_modified)
dec_resp = client.post(f'/v1/runs/{tid}/decision', headers=COM, json=signed_cli_hmac(ch_modified))
print('Test 8 Response:', dec_resp.status_code, dec_resp.json())

# Test 16: Cross-package
resp2 = client.post('/v1/runs', headers=SVC, json={'club_id': '330323'})
tid2 = resp2.json()['thread_id']
ch2 = client.post(f'/v1/runs/{tid2}/decision-challenge', headers=COM, json=APPROVE_INTENT).json()
cross_package_payload = {**ch2, 'candidate_id': 'scout-best-p1', 'owner': 'scout'}
dec_resp2 = client.post(f'/v1/runs/{tid2}/decision', headers=COM, json=signed_cli_hmac(cross_package_payload))
print('Test 16 Response:', dec_resp2.status_code, dec_resp2.json())