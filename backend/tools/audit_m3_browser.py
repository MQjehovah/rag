"""Local-only browser acceptance harness; not a production entry point.
Environment overrides (defaults keep the 2026-09-08 layout unchanged):
  M3_REPORT_ROOT      report/log/seed root   (default backend/reports/m3-browser-20260908)
  M3_BUSINESS_DB      business sqlite file    (default <report root>/business.db)
  M3_EXPERIMENT_ROOT  experiment root         (default <report root>/lab)
  M3_STUB_URL         local model stub base   (default http://127.0.0.1:<stub port>/chat)
  M3_BACKEND_PORT     uvicorn port            (default 18761)
  M3_STUB_PORT        stub HTTP port          (default 18763)
"""
from pathlib import Path
import os, sys, json, time, threading, importlib.util

BACKEND = Path(__file__).resolve().parents[1]
_DEF_REPORT = BACKEND / 'reports' / 'm3-browser-20260908'
ROOT = Path(os.environ.get('M3_REPORT_ROOT') or _DEF_REPORT)
ROOT.mkdir(parents=True, exist_ok=True)
os.chdir(ROOT)  # never load the project's .env in this harness
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / 'tests'))
M3_BUSINESS_DB = Path(os.environ.get('M3_BUSINESS_DB') or (ROOT / 'business.db'))
M3_LAB_ROOT = Path(os.environ.get('M3_EXPERIMENT_ROOT') or (ROOT / 'lab'))
M3_BACKEND_PORT = int(os.environ.get('M3_BACKEND_PORT') or '18761')
M3_STUB_PORT = int(os.environ.get('M3_STUB_PORT') or '18763')
URL = os.environ.get('M3_STUB_URL') or f'http://127.0.0.1:{M3_STUB_PORT}/chat'
vals = {
    'DATABASE_URL': 'sqlite:///' + M3_BUSINESS_DB.as_posix(),
    'JWT_SECRET_KEY': 'local-browser-fixture-signing-key-not-production',
    'LOCAL_ADMIN_PASSWORD': 'AuditFixture-Only-2026',
    'LLM_API_URL': URL, 'LLM_API_KEY': 'stub-key', 'LLM_MODEL': 'executor-stub',
    'WIKISKILL_CONSOLE_ENABLED': 'true',
    'WIKISKILL_EVOLUTION_ADMIN_ENABLED': 'true',
    'WIKISKILL_EVOLUTION_ADMIN_REAL_ENABLED': 'true',
    'WIKISKILL_BUSINESS_COMPILE_ENABLED': 'false',
    'WIKISKILL_PROMOTION_ENV': 'isolated-test',
    'WIKISKILL_EVOLUTION_ALLOW_SIMULATED_PROMOTION': 'true',
    'WIKISKILL_CONSOLE_ROOTS': json.dumps({'browser-lab': str(M3_LAB_ROOT)}),
    'WIKISKILL_REVIEWER_MODEL_ID': 'reviewer-stub',
    'WIKISKILL_REVIEWER_API_URL': URL,
    'WIKISKILL_REVIEWER_PROMPT_VERSION': 'prompt-v1',
    'WIKISKILL_REVIEWER_RETRIES': '2', 'WIKISKILL_REVIEWER_TIMEOUT': '30',
    'WIKISKILL_CREDENTIAL_PROVIDERS': json.dumps({'browser-fixture': {
        'credential_env': 'M3_STUB_KEY', 'endpoints': [URL], 'allow_insecure': True}}),
    'WIKISKILL_DEFAULT_PROVIDER': 'browser-fixture',
    'WIKISKILL_REVIEWER_PROVIDER': 'browser-fixture',
    'WIKISKILL_REQUIRE_PROVIDER_BINDING': 'true',
    'WIKISKILL_ALLOWED_LLM_ENDPOINTS': '',
    'M3_STUB_KEY': 'stub-key',
}
os.environ.update(vals)

from app.config import settings
from app.main import app
from app.api.deps import get_shared_engine
from app.models.database import User, UserGroup
from app.models import evolution as ev
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.api.auth import pwd_context
from app.core.skill_evolution import control, runenv, skill_store, business_ops as bops
import test_skill_evolution_stage8n as fixture

spec = importlib.util.spec_from_file_location('browser_e3', BACKEND/'tests/test_skill_evolution_stage8h_e3.py')
e3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e3)
from http.server import ThreadingHTTPServer
class Handler(e3._E3Handler):
    def do_POST(self):
        time.sleep(0.4)  # bounded response latency, not a change to application scheduling
        super().do_POST()
        (ROOT/'stub-counts.json').write_text(json.dumps(e3._E3Handler.role_counts), encoding='utf-8')
    def log_message(self, *args):
        pass
stub = ThreadingHTTPServer(('127.0.0.1', M3_STUB_PORT), Handler)
threading.Thread(target=stub.serve_forever,daemon=True).start()

engine=get_shared_engine()
ev.metadata.create_all(engine)
with Session(engine) as db:
    if not db.query(User).filter_by(username='audit-admin').first():
        for name,group in [('audit-admin','__local_admin__'),('audit-reader','audit-readers')]:
            db.add(User(id=name,username=name,display_name=name,is_local=True,is_active=True,
                        password_hash=pwd_context.hash('AuditFixture-Only-2026')))
            db.add(UserGroup(id=name+'-g',user_id=name,group_name=group))
        db.execute(text("INSERT INTO wiki_workspaces (id,key,name,acl_scope,scope_id,status) VALUES ('ws-prod-1','browser-fixture','ISOLATED browser fixture','public','public','active')"))
        db.commit()

lab=runenv.ensure_experiment_root(M3_LAB_ROOT)
seedfile=ROOT/'seed.json'
if not seedfile.exists():
    simulated=control.create_experiment_and_run(lab,dataset_version='wiki-default-v3',max_iterations=2)
    real=control.create_experiment_and_run(lab,dataset_version='wiki-default-v2dev',model_mode='real',review='v2',max_iterations=2,
         budget={'max_model_calls':260,'max_tool_calls':130,'max_seconds':1800})
    with skill_store.session_for(lab) as db, Session(engine) as biz:
        a=fixture._add_ver(db,'browser-a',1,'<script>window.M3_XSS=1</script>\n审查指令 A')
        b=fixture._add_ver(db,'browser-b',1,'审查指令 B')
        one=fixture._accepted([fixture._claim(a)])
        two=fixture._accepted([fixture._claim(a),fixture._claim(b)])
        # Initial synthetic history only; never record it as real/calibrated evidence.
        bops.promote(biz,db,fixture._exp_row('fixture-initial-one',one),workspace_id='ws-prod-1',created_by='fixture-seed',allow_simulated=True,idempotency_key='fixture-one')
        bops.promote(biz,db,fixture._exp_row('fixture-initial-two',two),workspace_id='ws-prod-1',created_by='fixture-seed',allow_simulated=True,idempotency_key='fixture-two')
        row=db.get(ev.EvolutionExperiment,simulated['experiment_id'])
        row.current_skill_set_json=json.dumps(two,ensure_ascii=False)
        row.best_skill_set_json=json.dumps(two,ensure_ascii=False)
        row.best_score_passed=1; row.best_score_total=2
        db.commit()
    seedfile.write_text(json.dumps({'simulated':simulated,'real':real},ensure_ascii=False,indent=2),encoding='utf-8')

# Transparent local logging/fault injection: application executes normally.
class AuditTransport:
    def __init__(self, app): self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http': return await self.app(scope,receive,send)
        chunks=[]
        async def recv():
            msg=await receive()
            if msg['type']=='http.request': chunks.append(msg.get('body',b''))
            return msg
        messages=[]
        async def capture(msg): messages.append(msg)
        await self.app(scope,recv,capture)
        path=scope['path']; status=next((m['status'] for m in messages if m['type']=='http.response.start'),0)
        raw=b''.join(chunks)
        body=None
        if path.startswith('/api/evolution') and raw:
            try: body=json.loads(raw)
            except Exception: body='non-json'
        record={'time':time.time(),'method':scope['method'],'path':path,'status':status,'body':body}
        if path.startswith('/api/evolution'):
            record['response']=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body').decode('utf-8','replace')
        arm=ROOT/'drop-next-rollback.flag'
        if path.endswith('/business/rollback') and scope['method']=='POST' and status==200 and arm.exists():
            arm.unlink(); record['fault']='successful response replaced by 502 (local transport fixture)'
            messages=[{'type':'http.response.start','status':502,'headers':[(b'content-type',b'application/json')]},
                      {'type':'http.response.body','body':b'{"detail":"LOCAL TEST: upstream response lost after commit"}'}]
        with (ROOT/'http-audit.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(record,ensure_ascii=False)+'\n')
        for msg in messages: await send(msg)

if __name__=='__main__':
    import uvicorn
    print(f'ISOLATED browser fixture ready: backend {M3_BACKEND_PORT}, stub {M3_STUB_PORT}, report {ROOT}',flush=True)
    uvicorn.run(AuditTransport(app),host='127.0.0.1',port=M3_BACKEND_PORT,lifespan='off',access_log=False)
