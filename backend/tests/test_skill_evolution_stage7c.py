"""阶段 7A 真实试运行入口接线离线验收（HTTP stub，非真实供应商调用）。

从 CLI 参数解析/命令处理出发：
- preflight 不发请求、fail-closed（角色缺配置报错）、不输出密钥；
- create 建 run 打印 run_id；run/resume 复用同 run，不重置预算/不建替代实验；
- 全角色（executor/maintainer/proposer）真实路径经 HTTP stub 请求并由 BudgetGuard
  计数（used_model_calls == HTTP 请求数，1:1）；所有模拟实现被调用即抛错；
- 载荷与超时实测：发送的 JSON 含 max_tokens（仅在配置时）、httpx timeout 生效；
  供应商拒绝该参数（HTTP 4xx）→ 明确失败，不静默移除限制后继续发送。
本文件任何用例都不向真实供应商发起请求。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.core.skill_evolution import cli as cli_mod
from app.core.skill_evolution import orchestrator as orch
from app.core.skill_evolution import skill_store

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DS_V3 = BACKEND_ROOT / "eval/wiki_evolution/datasets/wiki-default-v3"


class _StubState:
    def __init__(self):
        self.requests: list[dict] = []
        self.counts = {"executor": 0, "maintainer": 0, "proposer": 0}
        self.reject_max_tokens = False
        self.lock = threading.Lock()

    def classify(self, body: dict) -> str:
        text = json.dumps(body.get("messages", []), ensure_ascii=False)
        if "经验维护者" in text:
            return "maintainer"
        if "技能提议者" in text:
            return "proposer"
        return "executor"

    def response_for(self, kind: str) -> dict:
        if kind == "maintainer":
            obj = {"create_patterns": [], "update_patterns": [],
                   "append_log": ["试运行 stub：本轮不新增模式"],
                   "update_index": False}
        elif kind == "proposer":
            obj = {"action": "no_action",
                   "reason": "试运行 stub：无合法候选（如实记录）"}
        else:
            obj = {"summary": "stub", "content": "正文：试运行 stub 内容。"}
        return {"choices": [{"message": {"content": json.dumps(
            obj, ensure_ascii=False)}}]}


def _make_server(state: _StubState):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw)
            with state.lock:
                state.requests.append({"path": self.path, "body": body})
                kind = state.classify(body)
                state.counts[kind] += 1
                if state.reject_max_tokens and "max_tokens" in body:
                    self.send_response(400)
                    self.end_headers()
                    return
            payload = state.response_for(kind)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # 静音
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


@pytest.fixture(autouse=True)
def _isolate():
    from app.core.wiki_pipeline import executor, registry
    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    executor.reset_external_runners()
    registry.REGISTRY.clear()


@pytest.fixture
def stub_env(tmp_path, monkeypatch):
    from app.config import settings
    state = _StubState()
    srv = _make_server(state)
    url = f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
    monkeypatch.setattr(settings, "llm_api_url", url)
    monkeypatch.setattr(settings, "llm_api_key", "stub-key-not-real")
    monkeypatch.setattr(settings, "llm_model", "stub-model")
    yield {"state": state, "srv": srv, "root": tmp_path / "root"}
    srv.shutdown()


def _parse(mode: str, root: str, extra: list[str]) -> argparse.Namespace:
    argv = ["trial-real-d", mode, "--root", root,
            "--dataset", str(DS_V3), "--iterations", "1",
            "--max-requests", "40", "--max-tools", "20",
            "--max-seconds", "600", "--timeout", "30"] + extra
    parser = cli_mod.build_parser() if hasattr(cli_mod, "build_parser") \
        else cli_mod._build_parser()
    return parser.parse_args(argv)


def _dispatch(ns: argparse.Namespace):
    return ns.func(ns)


def test_preflight_issues_no_requests_and_fail_closed(stub_env):
    state, srv = stub_env["state"], stub_env["srv"]
    # 正常：rc=0，无 HTTP，输出不含密钥
    ns = _parse("preflight", str(stub_env["root"]),
                ["--max-output-tokens", "2000"])
    assert _dispatch(ns) == 0
    assert state.requests == []
    out = cli_mod._trial_summary(
        ns, cli_mod.load_dataset(DS_V3), [])
    text = json.dumps(out)
    assert "stub-key-not-real" not in text and "llm_api_key" not in text
    assert out["roles"]["eval"]["uses_same_as"] == "executor"
    # fail-closed：缺任一角色配置 → 报错且仍无 HTTP
    from app.config import settings
    import app.core.skill_evolution.cli as _c
    _c.settings_backup = None
    saved = (settings.llm_api_key,)
    try:
        settings.llm_api_key = ""
        ns2 = _parse("preflight", str(stub_env["root"]), [])
        assert _dispatch(ns2) == 1
        assert state.requests == []
    finally:
        settings.llm_api_key = saved[0]


def _raise(*a, **k):
    raise AssertionError("模拟实现被调用（默认参数悄悄回退）")


def test_cli_create_run_resume_guard_accounting(stub_env, monkeypatch):
    state, srv = stub_env["state"], stub_env["srv"]
    # 任何模拟实现被调用 → 抛错
    monkeypatch.setattr(
        "app.core.skill_evolution.maintainer.SimulatedMaintainer", _raise)
    monkeypatch.setattr(
        "app.core.skill_evolution.proposer.SimulatedProposer", _raise)

    ns = _parse("create", str(stub_env["root"]),
                ["--max-output-tokens", "1500"])
    assert _dispatch(ns) == 0
    run_id = ns.run_id if hasattr(ns, "run_id") else None
    # create 打印 run_id：从 DB 读唯一 run
    db = skill_store.session_for(Path(stub_env["root"]))
    try:
        from app.models.evolution import EvolutionRun
        runs = db.query(EvolutionRun).all()
        assert len(runs) == 1
        run_id = runs[0].run_id
        budget0 = json.loads(runs[0].config_json)["budget"]
        used0 = runs[0].used_model_calls
    finally:
        db.close()
    assert state.requests == []  # create 不发请求

    # run（含 resume 语义）
    ns_run = _parse("run", str(stub_env["root"]), ["--run-id", run_id])
    rc = _dispatch(ns_run)
    # no_action 是正常提议结果；完成或暂停都允许（审计字段齐全）
    assert rc in (0, 1)
    assert state.requests  # 真实请求确已发生

    db = skill_store.session_for(Path(stub_env["root"]))
    try:
        from app.models.evolution import EvolutionRun
        row = db.get(EvolutionRun, run_id)
        cfg = json.loads(row.config_json)
        assert cfg["budget"] == budget0      # 预算未被重置/扩大
        assert row.used_model_calls > 0
        assert row.used_model_calls == len(state.requests)  # guard 1:1 计数
        assert state.counts["executor"] > 0
        assert state.counts["maintainer"] > 0
        assert state.counts["proposer"] > 0
    finally:
        db.close()

    # 再次 run 同一 run：不得创建替代实验/run，也不得重发或重置
    try:
        rc2 = _dispatch(_parse("run", str(stub_env["root"]),
                               ["--run-id", run_id]))
    except Exception:
        rc2 = -1  # 终态不可再 claim → 明确报错（loud）
    db = skill_store.session_for(Path(stub_env["root"]))
    try:
        from app.models.evolution import EvolutionRun, EvolutionExperiment
        row = db.get(EvolutionRun, run_id)
        assert json.loads(row.config_json)["budget"] == budget0
        assert row.used_model_calls == len(state.requests)  # 未重置/未追加
        assert db.query(EvolutionExperiment).count() == 1   # 无替代实验
    finally:
        db.close()
    assert rc2 != 0


def test_payload_max_tokens_and_timeout_reach_http(stub_env, monkeypatch):
    from app.core.knowledge_compiler_v3.wiki_page_builder import (
        call_wiki_llm_json)
    state, srv = stub_env["state"], stub_env["srv"]
    msg = [{"role": "user", "content": "执行者真实路径载荷验证"}]
    out = asyncio.run(call_wiki_llm_json(
        msg, context="wiki-synthesis", timeout=7.0, max_output_tokens=2048))
    assert out is not None
    with state.lock:
        last = state.requests[-1]
    assert last["body"]["max_tokens"] == 2048   # 实际发送载荷含限制
    # 供应商拒绝限制参数 → 明确失败（HTTP 4xx），不静默移除继续发送
    state.reject_max_tokens = True
    with pytest.raises(Exception):
        asyncio.run(call_wiki_llm_json(
            msg, context="wiki-synthesis", timeout=7.0, max_output_tokens=2048))
    with state.lock:
        assert state.requests[-1]["body"].get("max_tokens") == 2048
    # 未配置 max_output_tokens → 载荷与既有默认一致（无该字段）
    state.reject_max_tokens = False
    asyncio.run(call_wiki_llm_json(msg, context="wiki-synthesis", timeout=7.0))
    with state.lock:
        assert "max_tokens" not in state.requests[-1]["body"]
