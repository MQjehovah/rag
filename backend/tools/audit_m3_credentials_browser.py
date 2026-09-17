"""凭据绑定终审的浏览器隔离夹具（不进入生产入口）。

与 audit_m3_browser.py 相同参数化（M3_REPORT_ROOT/M3_BUSINESS_DB/M3_EXPERIMENT_ROOT/
M3_BACKEND_PORT/M3_STUB_PORT），区别：
- 受控 provider 两个：executor-fixture（credential_env=EXECUTOR_FIXTURE_KEY）与
  reviewer-fixture（credential_env=REVIEWER_FIXTURE_KEY），端点均为本地 stub；
- settings.llm_api_key 置为 EVIL-GLOBAL-MUST-NOT-BE-USED（用于证明绝无回退）；
- stub 对每个请求按角色断言 Authorization：executor/maintainer/proposer 必须是
  EXECUTOR key，reviewer 必须是 REVIEWER key；任何不匹配/缺失 → 500 且不计数；
  断言结果只落 计数与布尔（stub-credential-assert.json），绝不落密钥值。
"""
from pathlib import Path
import os, sys, json, time, threading, importlib.util

BACKEND = Path(__file__).resolve().parents[1]
_DEF_REPORT = BACKEND / 'reports' / 'm3-credential-browser'
ROOT = Path(os.environ.get('M3_REPORT_ROOT') or _DEF_REPORT)
ROOT.mkdir(parents=True, exist_ok=True)
os.chdir(ROOT)
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / 'tests'))
M3_BUSINESS_DB = Path(os.environ.get('M3_BUSINESS_DB') or (ROOT / 'business.db'))
M3_LAB_ROOT = Path(os.environ.get('M3_EXPERIMENT_ROOT') or (ROOT / 'lab'))
M3_BACKEND_PORT = int(os.environ.get('M3_BACKEND_PORT') or '28771')
M3_STUB_PORT = int(os.environ.get('M3_STUB_PORT') or '28773')

EXEC_KEY_ENV = 'EXECUTOR_FIXTURE_KEY'
REV_KEY_ENV = 'REVIEWER_FIXTURE_KEY'
EXEC_KEY = os.environ.get(EXEC_KEY_ENV) or 'EXECUTOR-SECRET-MUST-NOT-LEAK'
REV_KEY = os.environ.get(REV_KEY_ENV) or 'REVIEWER-SECRET-MUST-NOT-LEAK'
EVIL_GLOBAL = 'EVIL-GLOBAL-MUST-NOT-BE-USED'
URL = f'http://127.0.0.1:{M3_STUB_PORT}/chat'
vals = {
    'DATABASE_URL': 'sqlite:///' + M3_BUSINESS_DB.as_posix(),
    'JWT_SECRET_KEY': 'local-credential-fixture-signing-key-not-production',
    'LOCAL_ADMIN_PASSWORD': 'CredentialFixture-Only-2026',
    'LLM_API_URL': URL, 'LLM_API_KEY': EVIL_GLOBAL, 'LLM_MODEL': 'executor-stub',
    'WIKISKILL_CONSOLE_ENABLED': 'true',
    'WIKISKILL_EVOLUTION_ADMIN_ENABLED': 'true',
    'WIKISKILL_EVOLUTION_ADMIN_REAL_ENABLED': 'true',
    'WIKISKILL_BUSINESS_COMPILE_ENABLED': 'false',
    'WIKISKILL_PROMOTION_ENV': 'isolated-test',
    'WIKISKILL_CONSOLE_ROOTS': json.dumps({'browser-lab': str(M3_LAB_ROOT)}),
    'WIKISKILL_REVIEWER_MODEL_ID': 'reviewer-stub',
    'WIKISKILL_REVIEWER_API_URL': URL,
    'WIKISKILL_REVIEWER_PROMPT_VERSION': 'prompt-v1',
    'WIKISKILL_REVIEWER_RETRIES': '2', 'WIKISKILL_REVIEWER_TIMEOUT': '30',
    'WIKISKILL_CREDENTIAL_PROVIDERS': json.dumps({
        'executor-fixture': {'credential_env': EXEC_KEY_ENV,
                             'endpoints': [URL], 'allow_insecure': True},
        'reviewer-fixture': {'credential_env': REV_KEY_ENV,
                             'endpoints': [URL], 'allow_insecure': True}}),
    'WIKISKILL_DEFAULT_PROVIDER': 'executor-fixture',
    'WIKISKILL_REVIEWER_PROVIDER': 'reviewer-fixture',
    'WIKISKILL_REQUIRE_PROVIDER_BINDING': 'true',
    'WIKISKILL_ALLOWED_LLM_ENDPOINTS': '',
    EXEC_KEY_ENV: EXEC_KEY, REV_KEY_ENV: REV_KEY,
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
from app.core.skill_evolution import control, runenv

spec = importlib.util.spec_from_file_location(
    'cred_e3', BACKEND / 'tests' / 'test_skill_evolution_stage8h_e3.py')
e3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e3)
from http.server import ThreadingHTTPServer

_EXPECTED_AUTH = {
    e3.ROLE_EXEC: 'Bearer ' + EXEC_KEY,
    e3.ROLE_MAINTAIN: 'Bearer ' + EXEC_KEY,
    e3.ROLE_PROPOSE: 'Bearer ' + EXEC_KEY,
    e3.ROLE_REVIEW: 'Bearer ' + REV_KEY,
}
_CRED_LOG = ROOT / 'stub-credential-assert.json'


def _log_cred(role, ok: bool):
    state = {}
    if _CRED_LOG.exists():
        try:
            state = json.loads(_CRED_LOG.read_text(encoding='utf-8'))
        except Exception:
            state = {}
    entry = state.get(role, {'ok': 0, 'mismatch': 0})
    entry['ok' if ok else 'mismatch'] += 1
    state[role] = entry
    _CRED_LOG.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                         encoding='utf-8')


class Handler(e3._E3Handler):
    def do_POST(self):  # noqa: N802
        import io
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        body = json.loads(raw or b'{}')
        messages = body.get('messages') or []
        prompt = '\n'.join(str(m.get('content') or '') for m in messages
                           if isinstance(m, dict) and m.get('role') == 'user')
        sys_text = '\n'.join(str(m.get('content') or '') for m in messages
                             if isinstance(m, dict) and m.get('role') == 'system')
        role, _content = e3._route(prompt=prompt, sys_text=sys_text)
        auth = self.headers.get('Authorization')
        expected = _EXPECTED_AUTH.get(role)
        if auth != expected:
            _log_cred(role, False)
            out = b'{"error":"LOCAL credential mismatch (role-key isolation)"}'
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        _log_cred(role, True)
        # 委托 e3 处理（计数/角色应答），但回填已读 body 避免二次读取挂起
        self.rfile = io.BytesIO(raw)
        super().do_POST()

    def log_message(self, *args):
        pass


stub = ThreadingHTTPServer(('127.0.0.1', M3_STUB_PORT), Handler)
threading.Thread(target=stub.serve_forever, daemon=True).start()

engine = get_shared_engine()
ev.metadata.create_all(engine)
with Session(engine) as db:
    if not db.query(User).filter_by(username='audit-admin').first():
        for name, group in [('audit-admin', '__local_admin__'),
                            ('audit-reader', 'audit-readers')]:
            db.add(User(id=name, username=name, display_name=name,
                        is_local=True, is_active=True,
                        password_hash=pwd_context.hash(
                            'CredentialFixture-Only-2026')))
            db.add(UserGroup(id=name + '-g', user_id=name, group_name=group))
        db.execute(text("INSERT INTO wiki_workspaces (id,key,name,acl_scope,"
                        "scope_id,status) VALUES ('ws-creds-1','browser-creds',"
                        "'CREDENTIAL browser fixture','public','public','active')"))
        db.commit()

lab = runenv.ensure_experiment_root(M3_LAB_ROOT)
seedfile = ROOT / 'seed.json'
if not seedfile.exists():
    simulated = control.create_experiment_and_run(
        lab, dataset_version='wiki-default-v3', max_iterations=2)
    real = control.create_experiment_and_run(
        lab, dataset_version='wiki-default-v2dev', model_mode='real',
        review='v2', max_iterations=2,
        budget={'max_model_calls': 260, 'max_tool_calls': 130,
                'max_seconds': 1800})
    seedfile.write_text(
        json.dumps({'simulated': simulated, 'real': real},
                   ensure_ascii=False, indent=2), encoding='utf-8')


class AuditTransport:
    """脱敏 HTTP 审计（仅 /api/evolution* 路径；无登录体/头/密钥）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        chunks = []

        async def recv():
            msg = await receive()
            if msg['type'] == 'http.request':
                chunks.append(msg.get('body', b''))
            return msg
        messages = []

        async def capture(msg):
            messages.append(msg)
        await self.app(scope, recv, capture)
        path = scope['path']
        status = next((m['status'] for m in messages
                       if m['type'] == 'http.response.start'), 0)
        raw = b''.join(chunks)
        body = None
        if path.startswith('/api/evolution') and raw:
            try:
                body = json.loads(raw)
            except Exception:
                body = 'non-json'
        record = {'time': time.time(), 'method': scope['method'],
                  'path': path, 'status': status, 'body': body}
        if path.startswith('/api/evolution'):
            record['response'] = b''.join(
                m.get('body', b'') for m in messages
                if m['type'] == 'http.response.body').decode(
                    'utf-8', 'replace')
        with (ROOT / 'http-audit.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        for msg in messages:
            await send(msg)


if __name__ == '__main__':
    import uvicorn
    print(f'CREDENTIAL browser fixture ready: backend {M3_BACKEND_PORT}, '
          f'stub {M3_STUB_PORT}, report {ROOT}', flush=True)
    uvicorn.run(AuditTransport(app), host='127.0.0.1', port=M3_BACKEND_PORT,
                lifespan='off', access_log=False)
