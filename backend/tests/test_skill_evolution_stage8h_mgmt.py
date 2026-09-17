"""审计 2：管理 API 创建/确认启动 → 独立 worker（子进程）→ 本地 HTTP stub 两轮闭环。

同一次运行保存同一 run_id 全链证据（管理请求、worker、各角色 HTTP 请求计数、
真实评分、accepted/rejected 事件、下一轮注入版本+正文哈希）。
与 stage8h_e3 的“编排器直连”互为组合验证；本文件证明管理入口实际拉起独立 worker
并完成同一两轮闭环。所有网络仅限本地 stub；凭据虚构；不写 Evaluation/门控/分数。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.jwt_utils import get_current_user

BACKEND = Path(__file__).resolve().parent.parent
DEV_DS = BACKEND / "eval/wiki_evolution/datasets/wiki-default-v2dev"

# 复用 stage8h_e3 的协议 stub（同一 handler/路由实现，避免两份漂移）
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "_e3stub", str(BACKEND / "tests" / "test_skill_evolution_stage8h_e3.py"))
_e3 = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_e3)
_E3Handler = _e3._E3Handler
ROLE_EXEC, ROLE_MAINTAIN = _e3.ROLE_EXEC, _e3.ROLE_MAINTAIN
ROLE_PROPOSE, ROLE_REVIEW = _e3.ROLE_PROPOSE, _e3.ROLE_REVIEW



def _admin(app, value):
    if value is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = lambda: value


def _set_child_env(monkeypatch, url: str):
    """独立 worker（子进程）从环境变量读取配置（虚构凭据 + 受控 provider）。"""
    vals = {
        "LLM_API_URL": url, "LLM_API_KEY": "stub-key", "LLM_MODEL": "executor-stub",
        "WIKISKILL_REVIEWER_MODEL_ID": "reviewer-stub",
        "WIKISKILL_REVIEWER_API_URL": url,
        "WIKISKILL_REVIEWER_PROMPT_VERSION": "prompt-v1",
        "WIKISKILL_REVIEWER_RETRIES": "2", "WIKISKILL_REVIEWER_TIMEOUT": "30",
        "WIKISKILL_EVOLUTION_ADMIN_REAL_ENABLED": "true",
        "WIKISKILL_CREDENTIAL_PROVIDERS": json.dumps({
            "e2e": {"credential_env": "FK_E2E",
                    "endpoints": [url], "allow_insecure": True}}),
        "WIKISKILL_DEFAULT_PROVIDER": "e2e",
        "WIKISKILL_REVIEWER_PROVIDER": "e2e",
        "WIKISKILL_REQUIRE_PROVIDER_BINDING": "true",
        "FK_E2E": "stub-key",
    }
    for k, v in vals.items():
        monkeypatch.setenv(k, v)


def test_management_single_chain_worker_two_rounds(tmp_path, monkeypatch):
    from app.main import app
    from app.core.skill_evolution import control as _ctl, runenv, skill_store
    from app.core.skill_evolution.review_eval import dataset_fingerprint
    import sqlalchemy as sa
    # 强制独立子进程 worker（防御其它文件残留线程 spawner）
    _ctl.set_worker_spawner(_ctl.SUBPROCESS_SPAWNER)

    _E3Handler.calls = []
    _E3Handler.role_counts = {}
    _E3Handler.fail_next = 0
    from http.server import ThreadingHTTPServer
    import threading as _t
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _E3Handler)
    _t.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/chat"

    root = runenv.ensure_experiment_root(tmp_path / "lab")
    business = tmp_path / "business.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{(business).as_posix()}")
    monkeypatch.setattr(settings, "wikiskill_console_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_real_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_console_roots",
                        json.dumps({"lab": str(root)}))
    monkeypatch.setattr(settings, "llm_api_url", url)
    monkeypatch.setattr(settings, "llm_api_key", "stub-key")

            # 正式 real provider 门禁：受控虚构 provider（键经非秘密 env 引用）
    _endpoints = [str(settings.llm_api_url or "")]
    if getattr(settings, "wikiskill_reviewer_api_url", None):
        _endpoints.append(str(settings.wikiskill_reviewer_api_url))
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        json.dumps({"e2e": {"credential_env": "FK_E2E",
                                            "endpoints": _endpoints,
                                            "allow_insecure": True}}))
    monkeypatch.setattr(settings, "wikiskill_default_provider", "e2e")
    monkeypatch.setattr(settings, "wikiskill_reviewer_provider", "e2e")
    monkeypatch.setattr(settings, "wikiskill_require_provider_binding", True)
    monkeypatch.setenv("FK_E2E", "stub-key")
    monkeypatch.setattr(settings, "llm_model", "executor-stub")
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "reviewer-stub")
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", url)
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version", "prompt-v1")
    monkeypatch.setattr(settings, "wikiskill_reviewer_timeout", 30.0)
    monkeypatch.setattr(settings, "wikiskill_reviewer_retries", 2)
    _set_child_env(monkeypatch, url)
    try:
        with TestClient(app) as client:
            _admin(app, {"id": "a", "groups": ["__local_admin__"]})
            created = client.post("/api/evolution-admin/experiments", json={
                "root": "lab", "dataset_version": "wiki-default-v2dev",
                "model_mode": "real", "max_iterations": 2,
                "max_model_calls": 260, "max_tool_calls": 130,
                "max_seconds": 1800, "experience": "full", "review": "v2",
            }).json()
            run_id = created["run_id"]
            exp_id = created["experiment_id"]
            assert created["status"] == "queued"
            assert created["model_mode"] == "real"
            # 无网络配置预览 → 确认完整指纹（不落盘、不发探测请求）
            pv = client.get(
                f"/api/evolution-admin/runs/{run_id}/start-preview",
                params={"root": "lab"})
            assert pv.status_code == 200
            preview = pv.json()
            assert preview["config_fingerprint"]
            assert "stub-key" not in json.dumps(preview)
            # 真实模式：显式确认（参数须与 run 记录一致 + 预览指纹）
            resp0 = client.post(
                f"/api/evolution-admin/runs/{run_id}/start",
                json={"root": "lab"})
            assert resp0.status_code == 409   # 未确认
            resp1 = client.post(
                f"/api/evolution-admin/runs/{run_id}/start", json={
                    "root": "lab", "explicit_confirm": True,
                    "confirm_config_fingerprint":
                        preview["config_fingerprint"],
                    "confirm_reviewer_fingerprint":
                        preview.get("reviewer_fingerprint"),
                    "confirm_dataset_version": "wiki-default-v2dev",
                    "confirm_max_iterations": 2,
                    "confirm_max_model_calls": 260,
                    "confirm_max_tool_calls": 130,
                    "confirm_max_seconds": 1800})
            assert resp1.status_code == 200, resp1.text
            info = resp1.json()
            assert info["status"] == "queued" and info["pid"]  # 独立子进程 worker
            worker_pid_a = info["pid"]
            # 重复 start：不产生第二个 worker（并发 CAS → 409）
            respDup = client.post(
                f"/api/evolution-admin/runs/{run_id}/start", json={
                    "root": "lab", "explicit_confirm": True,
                    "confirm_config_fingerprint":
                        preview["config_fingerprint"]})
            assert respDup.status_code == 409
            # 轮询至终态（worker 独立执行，不阻塞 API）
            from app.core.skill_evolution import control
            deadline = time.time() + 420
            final = None
            while time.time() < deadline:
                v = control.run_state(root, run_id)
                if v["status"] in ("completed", "failed", "paused",
                                   "cancelled", "budget_exhausted"):
                    final = v
                    break
                time.sleep(1.5)
            assert final is not None
            if final["status"] != "completed":
                import json as _J
                raise AssertionError(_J.dumps({
                    "status": final["status"], "reason": final.get("stop_reason"),
                    "iter": [{"n": i["number"], "st": i["status"],
                              "step": i["step"], "e": i.get("error_code"),
                              "m": (i.get("error_message") or "")[:200]}
                             for i in final["iterations"]]},
                    ensure_ascii=False))
            assert final["current_iteration"] == 2, final
            # worker 日志存在（独立进程证据）
            logf = root / "runs" / "_console" / f"{run_id}.log"
            assert logf.is_file() and logf.stat().st_size > 0
            # 各角色（executor/maintainer/proposer/reviewer）均有本地 stub HTTP 记录
            rc = dict(getattr(_E3Handler, "role_counts", {}) or {})
            assert rc.get("executor", 0) >= 5, rc
            assert rc.get("maintainer", 0) >= 1, rc
            assert rc.get("proposer", 0) >= 1, rc
            assert rc.get("reviewer", 0) >= 1, rc
            assert all(c.get("auth") == "Bearer stub-key"
                       for c in _E3Handler.calls)
            # 持久预算：成功场景无“未知发送/预留残留”→ used_model_calls ==
            # 实际出站 HTTP 总数（executor/maintainer/proposer/reviewer 均经
            # guard.wrap 计数，finish sent_known=True）。
            used = (final or {}).get("used") or {}
            used_calls = int(used.get("model_calls") or 0)
            total_http = sum((rc or {}).values())
            assert total_http > 0
            assert used_calls == total_http, f"used={used_calls} http={total_http}"
            in_flight = skill_store.session_for(root).execute(sa.text(
                "SELECT reserved_in_flight_json FROM evolution_runs WHERE "
                "run_id=:r"), {"r": run_id}).fetchone()[0]
            assert json.loads(in_flight or "[]") == []
            reconciliation = {
                "used_model_calls": used_calls,
                "total_stub_http": total_http,
                "role_counts": dict(rc),
                "balance": "exact (all sent_known, no reserve leftover)"}
            assert int(worker_pid_a) > 0
            _admin(app, None)

        # 同一 run_id 的评分与门控证据（真实代码判定）
        db = skill_store.session_for(root)
        try:
            rows = db.execute(sa.text(
                "SELECT kind, valid, main_passed, main_total, "
                "per_task_results_json FROM evolution_evaluations "
                "WHERE experiment_id=:e ORDER BY created_at"),
                {"e": exp_id}).fetchall()
            base = [r for r in rows if r.kind == "baseline"][0]
            cands = [r for r in rows if r.kind == "candidate"]
            assert base.main_total == 4 and base.main_passed == 1
            assert len(cands) == 2
            assert all(c.valid == 1 for c in cands)
            assert [c.main_passed for c in cands] == [2, 2]
            events = db.execute(sa.text(
                "SELECT decision, candidate_version_ids_json FROM "
                "evolution_gate_events WHERE experiment_id=:e "
                "ORDER BY created_at"), {"e": exp_id}).fetchall()
            assert [e.decision for e in events] == ["accepted", "rejected"]
            it2 = db.execute(sa.text(
                "SELECT freeze_set_json FROM evolution_iterations WHERE "
                "run_id=:r AND number=2"), {"r": run_id}).fetchone()
            info_g = db.execute(sa.text(
                "SELECT current_skill_set_json FROM evolution_experiments "
                "WHERE experiment_id=:e"), {"e": exp_id}).fetchone()
            cur = json.loads(info_g.current_skill_set_json)["members"][0]
            freeze2 = json.loads(it2.freeze_set_json)["members"][0]
            assert freeze2["version_id"] == cur["version_id"]
            assert freeze2["content_hash"] == cur["content_hash"]
        finally:
            db.close()

        audit_rows = db2_rows if False else None
        # 审计输出（HTTP stub 工程验证）
        print(json.dumps({
            "kind": "http-stub-management-single-chain",
            "dataset_version": "wiki-default-v2dev",
            "dataset_fingerprint": dataset_fingerprint(DEV_DS),
            "val_task_ids": _val_ids(),
            "run_id": run_id,
            "experiment_id": exp_id,
            "worker_log": str(logf),
            "role_http_counts": dict(_E3Handler.role_counts),
            "baseline": {"valid": True, "passed": 1, "total": 4},
            "candidates_main_passed": [2, 2],
            "gate_decisions": ["accepted", "rejected"],
            "round2_injected_version": cur["version_id"],
            "round2_injected_hash": cur["content_hash"],
            "marker_requests": len([c for c in _E3Handler.calls
                                    if "E3-40C" in json.dumps(
                                        c["body"].get("messages"),
                                        ensure_ascii=False)]),
            "per_task": [{
                "kind": r.kind,
                "verdicts": [x.get("verdict")
                             for x in json.loads(r.per_task_results_json)],
                "tasks": [x.get("task_id")
                          for x in json.loads(r.per_task_results_json)],
            } for r in rows],
        }, ensure_ascii=False, default=str))
    finally:
        srv.shutdown()
        srv.server_close()


def _val_ids():
    from app.core.skill_evolution.contracts import load_dataset
    ds = load_dataset(DEV_DS)
    return [t.task_id for t in ds.tasks if t.split == "val"]


def test_management_subprocess_pause_resume_same_run(tmp_path, monkeypatch):
    """B. 独立子进程 worker 的安全边界暂停与恢复（复用 A 设施，独立 run）。

    首轮 accepted 后、下一轮尚未调度时请求暂停：已发请求结束、暂停后不再调度新
    模型请求；resume 沿用原冻结配置与剩余预算，阶段不重复、轮次不重复、无双 worker。
    """
    from app.main import app
    from app.core.skill_evolution import control as _ctl, runenv, skill_store
    from app.core.skill_evolution.review_eval import dataset_fingerprint
    import sqlalchemy as sa
    _ctl.set_worker_spawner(_ctl.SUBPROCESS_SPAWNER)
    _E3Handler.calls = []
    _E3Handler.role_counts = {}
    _E3Handler.fail_next = 0
    from http.server import ThreadingHTTPServer
    import threading as _t
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _E3Handler)
    _t.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/chat"
    root = runenv.ensure_experiment_root(tmp_path / "lab")
    business = tmp_path / "business.db"
    monkeypatch.setattr(settings, "database_url",
                        f"sqlite:///{(business).as_posix()}")
    monkeypatch.setattr(settings, "wikiskill_console_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_real_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_console_roots",
                        json.dumps({"lab": str(root)}))
    monkeypatch.setattr(settings, "llm_api_url", url)
    monkeypatch.setattr(settings, "llm_api_key", "stub-key")
    monkeypatch.setattr(settings, "llm_model", "executor-stub")
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        json.dumps({"e2e": {"credential_env": "FK_E2E",
                                            "endpoints": [url],
                                            "allow_insecure": True}}))
    monkeypatch.setattr(settings, "wikiskill_default_provider", "e2e")
    monkeypatch.setattr(settings, "wikiskill_reviewer_provider", "e2e")
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "reviewer-stub")
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", url)
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version", "prompt-v1")
    monkeypatch.setattr(settings, "wikiskill_reviewer_timeout", 30.0)
    monkeypatch.setattr(settings, "wikiskill_reviewer_retries", 2)
    _set_child_env(monkeypatch, url)
    fp_before = None
    try:
        with TestClient(app) as client:
            _admin(app, {"id": "a", "groups": ["__local_admin__"]})
            created = client.post("/api/evolution-admin/experiments", json={
                "root": "lab", "dataset_version": "wiki-default-v2dev",
                "model_mode": "real", "max_iterations": 2,
                "max_model_calls": 260, "max_tool_calls": 130,
                "max_seconds": 1800, "experience": "full", "review": "v2",
            }).json()
            run_id = created["run_id"]
            exp_id = created["experiment_id"]
            preview = client.get(
                f"/api/evolution-admin/runs/{run_id}/start-preview",
                params={"root": "lab"}).json()
            fp = preview["config_fingerprint"]
            rvf = preview.get("reviewer_fingerprint")
            start_body = {
                "root": "lab", "explicit_confirm": True,
                "confirm_config_fingerprint": fp,
                "confirm_reviewer_fingerprint": rvf,
                "confirm_dataset_version": "wiki-default-v2dev",
                "confirm_max_iterations": 2,
                "confirm_max_model_calls": 260,
                "confirm_max_tool_calls": 130,
                "confirm_max_seconds": 1800}
            r1 = client.post(f"/api/evolution-admin/runs/{run_id}/start",
                             json=start_body)
            assert r1.status_code == 200, r1.text
            pid1 = int(r1.json()["pid"])
            # 事件屏障：等首个 accepted 门控事件落库且 run 仍 running → 请求暂停；
            # worker 在轮边界（下一轮开始前）收敛为 paused（有界轮询，非固定 sleep）。
            deadline = time.time() + 300
            accepted_seen = False
            while time.time() < deadline:
                v = _ctl.run_state(root, run_id)
                n_acc = skill_store.session_for(root).execute(sa.text(
                    "SELECT count(*) FROM evolution_gate_events WHERE "
                    "experiment_id=:e AND decision='accepted'"),
                    {"e": exp_id}).fetchone()[0]
                if n_acc >= 1 and v["status"] == "running":
                    accepted_seen = True
                    break
                if v["status"] in ("completed", "failed", "budget_exhausted"):
                    raise AssertionError(
                        "worker 提前终态，未等到可暂停的安全边界: "
                        f"{v['status']}")
                time.sleep(0.05)
            assert accepted_seen
            # 立即请求暂停（轮 2 全程亚秒级，须尽快落 pause 标志）
            rp = client.post(f"/api/evolution-admin/runs/{run_id}/pause",
                             json={"root": "lab"})
            assert rp.status_code == 200, rp.text
            # 暂停收敛（worker 在安全边界退出；有界轮询；超时带脱敏诊断）
            deadline = time.time() + 420
            paused = False
            while time.time() < deadline:
                v = _ctl.run_state(root, run_id)
                if v["status"] == "paused":
                    paused = True
                    break
                if v["status"] in ("completed", "failed", "cancelled",
                                   "budget_exhausted"):
                    raise AssertionError(f"暂停后异常终态 {v['status']}")
                time.sleep(0.8)
            if not paused:
                import os as _os
                alive = True
                try:
                    _os.kill(pid1, 0)
                except OSError:
                    alive = False
                diag = {"worker_pid": pid1, "pid_alive": alive}
                raw = skill_store.session_for(root).execute(sa.text(
                    "SELECT status, pause_requested, current_iteration, "
                    "lease_owner, lease_token, lease_expires_at, error_code, "
                    "error_message FROM evolution_runs WHERE run_id=:r"),
                    {"r": run_id}).fetchone()
                diag["run_row"] = {k: (str(raw._mapping[k]) if raw._mapping[k]
                                       is not None else None)
                                   for k in raw._mapping.keys()}
                its = skill_store.session_for(root).execute(sa.text(
                    "SELECT number, status, step, error_code FROM "
                    "evolution_iterations WHERE run_id=:r ORDER BY number"),
                    {"r": run_id}).fetchall()
                diag["iterations"] = [tuple(x) for x in its]
                logf = root / "runs" / "_console" / f"{run_id}.log"
                tail = ""
                if logf.is_file():
                    tail = logf.read_text(encoding="utf-8",
                                          errors="replace")[-1500:]
                diag["worker_log_tail"] = tail
                diag["pause_api_ok"] = True
                diag["stub_requests"] = len(_E3Handler.calls)
                diag["last_stub_call"] = (_E3Handler.calls[-1]
                                          if _E3Handler.calls else None)
                raise AssertionError(
                    "暂停未收敛: " + json.dumps(diag, ensure_ascii=False,
                                                default=str))
            # 快照（暂停后、恢复前）：冻结配置与预算
            st_snap = _ctl.run_state(root, run_id)
            used_before = int((st_snap.get("used") or {}).get("model_calls")
                              or 0)
            fps_before = _ctl.runtime_fingerprints_of(root, run_id)
            fp_before = fps_before["config_fingerprint"]
            cfg_before = json.loads(
                skill_store.session_for(root).execute(sa.text(
                    "SELECT config_json FROM evolution_runs WHERE run_id=:r"),
                    {"r": run_id}).fetchone()[0])
            frozen_before = (cfg_before.get("runner") or {}).get("frozen")
            count_after_pause = len(_E3Handler.calls)
            time.sleep(3.0)
            assert len(_E3Handler.calls) == count_after_pause, \
                "暂停生效后仍调度了新模型请求"
            dbq = skill_store.session_for(root)
            try:
                n_events_paused = dbq.execute(sa.text(
                    "SELECT count(*) FROM evolution_gate_events WHERE "
                    "experiment_id=:e"), {"e": exp_id}).fetchone()[0]
            finally:
                dbq.close()
            assert n_events_paused == 1      # 仅 accepted；rejected 未在暂停前发生
            # 恢复：同一 run、确认同一冻结指纹
            rres = client.post(f"/api/evolution-admin/runs/{run_id}/resume",
                               json=start_body)
            assert rres.status_code == 200, rres.text
            pid2 = int(rres.json()["pid"])
            assert pid2 > 0 and pid2 != pid1     # 新 worker，但不同时存在
            deadline = time.time() + 420
            final = None
            while time.time() < deadline:
                v = _ctl.run_state(root, run_id)
                if v["status"] in ("completed", "failed", "cancelled",
                                   "budget_exhausted"):
                    final = v
                    break
                time.sleep(1.5)
            assert final is not None and final["status"] == "completed", \
                f"resume 未完成: {final and final['status']}"
            # 冻结配置不漂移
            fps_after = _ctl.runtime_fingerprints_of(root, run_id)
            assert fps_after["config_fingerprint"] == fp_before
            cfg_after = json.loads(
                skill_store.session_for(root).execute(sa.text(
                    "SELECT config_json FROM evolution_runs WHERE run_id=:r"),
                    {"r": run_id}).fetchone()[0])
            frozen_after = (cfg_after.get("runner") or {}).get("frozen")
            for key in ("model", "endpoint_host", "api_url",
                        "allowed_endpoints", "credential_env", "provider_id"):
                a = (frozen_before.get("roles") or {})
                b = (frozen_after.get("roles") or {})
                if key in (frozen_before.get("roles") or {}).get(
                        "executor", {}):
                    assert a["executor"].get(key) == b["executor"].get(key), key
            # 预算累计保留且恢复增量精确等于恢复后新增出站数（无未知/预留残留）
            used_final = int((final.get("used") or {}).get("model_calls") or 0)
            total_http_final = sum((dict(getattr(_E3Handler, "role_counts",
                                                {}) or {})).values())
            used_at_pause = used_before
            calls_at_pause = count_after_pause
            assert used_final == total_http_final, \
                f"used_final={used_final} http_final={total_http_final}"
            assert used_final - used_at_pause == \
                total_http_final - calls_at_pause, \
                (used_final - used_at_pause,
                 total_http_final - calls_at_pause)
            assert used_final >= used_before and used_before > 0
            dbq = skill_store.session_for(root)
            try:
                n_events = dbq.execute(sa.text(
                    "SELECT count(*) FROM evolution_gate_events WHERE "
                    "experiment_id=:e"), {"e": exp_id}).fetchone()[0]
                decisions = [r[0] for r in dbq.execute(sa.text(
                    "SELECT decision FROM evolution_gate_events WHERE "
                    "experiment_id=:e ORDER BY created_at"),
                    {"e": exp_id}).fetchall()]
                n_iters = dbq.execute(sa.text(
                    "SELECT count(*) FROM evolution_iterations WHERE "
                    "run_id=:r"), {"r": run_id}).fetchone()[0]
            finally:
                dbq.close()
            assert decisions == ["accepted", "rejected"]   # 不重复晋升
            assert n_events == 2 and n_iters == 2           # 轮次不重复
            _admin(app, None)
    finally:
        if _admin and False:
            pass
        srv.shutdown()
        srv.server_close()
    import io as _io
    out = _io.StringIO()
    out.write(json.dumps({
        "kind": "management-subprocess-pause-resume",
        "run_id": run_id, "experiment_id": exp_id,
        "worker_pid_start": pid1, "worker_pid_resume": pid2,
        "gate_decisions": ["accepted", "rejected"],
        "pause_http_calls_at_pause": count_after_pause,
        "used_model_calls_before_pause": used_before,
        "frozen_fingerprint_stable": fp_before == fps_after[
            "config_fingerprint"],
        "iterations": 2, "events": 2,
    }, ensure_ascii=False, default=str))
    print(out.getvalue())
