"""阶段 8A：演化实验只读管理控制台（后端）。

原则：
- 实验根目录仅来自服务端受控配置（settings.wikiskill_console_roots，{id: 绝对路径}）；
  客户端只能提交根标识与库内标识，绝不接受文件路径。
- 每次访问都校验登录 + 管理员；缺少可靠 workspace→业务授权映射时默认仅管理员可读。
- 只读打开实验库：sqlite3 mode=ro；库缺失/表缺失 → 明确 unavailable，
  绝不自动建库/建表/迁移（不调用 skill_store.session_for / schema 创建）。
- 不提供任何写操作；不返回密钥、认证头、完整敏感配置、原始资料或参考答案。
- 本模块不触发模型请求；内容一律结构化摘要，Markdown/正文不在后端渲染。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status

from app.config import settings
from app.core import access_control
from app.core.jwt_utils import get_current_user

# 8B：只读正文展示所需纯常量（不引入 skill_store/session，保持 mode=ro 直连）。
_EVOLUTION_SCHEMA_VERSION = "skill-evolution/v1"
_HASH_KEYS = ("skill_id", "domain", "runtime_ref", "schema_version",
              "skill_md", "purpose_md")

router = APIRouter(prefix="/api/evolution-console", tags=["evolution-console"])

_STORE_FILE = "skill_store.db"
_ID_RE = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-"
_RUNS_REL = "runs"


def _require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if not access_control.is_admin(current_user):
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN,
                            detail="需要管理员权限")
    return current_user


def _roots() -> dict[str, Path]:
    """服务端受控实验根映射；仅接受 {id: 绝对目录}。"""
    raw = (settings.wikiskill_console_roots or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500,
                            detail="实验根配置非法（非 JSON）") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="实验根配置必须是对象")
    out: dict[str, Path] = {}
    for kid, val in data.items():
        if not kid or any(c not in _ID_RE for c in kid) or len(kid) > 64:
            raise HTTPException(status_code=500, detail=f"根标识非法: {kid!r}")
        p = Path(str(val))
        if not p.is_absolute() or not p.is_dir():
            raise HTTPException(status_code=500,
                                detail=f"实验根不存在或非绝对目录: {kid!r}")
        out[kid] = p.resolve()
    return out


def _console_state() -> dict:
    if not settings.wikiskill_console_enabled:
        return {"console": "disabled", "reason": "功能未启用",
                "roots": []}
    roots = _roots()
    if not roots:
        return {"console": "unavailable",
                "reason": "未配置服务端实验根映射", "roots": []}
    return {"console": "ok", "reason": None,
            "roots": [{"id": k, "label": k} for k in sorted(roots)]}


def _state_guard(state: dict) -> None:
    if state["console"] != "ok":
        raise HTTPException(status_code=503,
                            detail=state["reason"] or "控制台不可用")


def _readonly_db(root_dir: Path):
    """sqlite3 mode=ro 连接；文件不存在/为空 → (None, reason)。"""
    db_file = root_dir / _STORE_FILE
    if not db_file.is_file() or db_file.stat().st_size == 0:
        return None, "实验库不存在或为空"
    uri = "file:" + urllib.parse.quote(db_file.resolve().as_posix()) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        # 库文件只读存在即可；查询失败将显式报错（不做任何建表/迁移）
        return conn, None
    except sqlite3.Error as exc:
        return None, f"实验库不可用: {exc}"


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    try:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503,
                            detail=f"实验库读取失败（未做任何修改）: {exc}") from exc


def _pick(row: dict, keys: tuple[str, ...]) -> dict:
    return {k: row.get(k) for k in keys if k in row}


_RUN_KEYS = ("run_id", "experiment_id", "workspace_id", "domain",
             "dataset_version", "init_mode", "status", "stop_reason",
             "max_iterations", "current_iteration",
             "used_model_calls", "used_tool_calls", "used_estimated_chars",
             "error_code", "error_message", "created_at", "started_at",
             "finished_at", "updated_at")
_ITER_KEYS = ("iteration_id", "run_id", "number", "status", "step",
              "proposal_id", "evaluation_id", "gate_event_id", "no_action",
              "attempts", "error_code", "error_message", "created_at",
              "updated_at")
_EXP_KEYS = ("experiment_id", "workspace_id", "domain", "dataset_version",
             "grader_version", "pipeline_key", "pipeline_version",
             "best_score_passed", "best_score_total", "status", "status_rev",
             "created_at", "updated_at")
_SKILL_KEYS = ("version_id", "skill_id", "seq", "domain", "parent_version_id",
               "content_hash", "source_type", "created_by", "created_at")
_PROP_KEYS = ("proposal_id", "run_id", "action", "skill_id",
              "parent_version_id", "candidate_version_id",
              "duplicate_of_version_id", "status", "created_at")
_EVAL_KEYS = ("evaluation_id", "kind", "proposal_id", "main_passed",
              "main_total", "valid", "invalid_reason", "created_at")


_SET_MEMBER_KEYS = ("skill_id", "version_id", "content_hash", "seq")


def sanitize_set(raw_json: str) -> dict:
    """技能集合只透出成员白名单字段，绝不透传正文/密钥/脚本。"""
    try:
        data = json.loads(raw_json or "{}")
    except ValueError:
        data = {}
    members = data.get("members") if isinstance(data, dict) else None
    if not isinstance(members, list):
        return {"members": []}
    return {"members": [{k: m.get(k) for k in _SET_MEMBER_KEYS
                         if k in m and isinstance(m, dict)}
                        for m in members]}


def _budget_summary(run_row: dict) -> dict:
    cfg = {}
    try:
        cfg = json.loads(run_row.get("config_json") or "{}")
    except ValueError:
        pass
    budget = cfg.get("budget") or {}
    runner = cfg.get("runner") or {}
    mode = runner.get("mode")
    if mode not in ("real", "simulated"):
        mode = "simulated" if not runner.get("real") else "real"
    return {
        "model_cap": budget.get("max_model_calls"),
        "tool_cap": budget.get("max_tool_calls"),
        "seconds_cap": budget.get("max_seconds"),
        "used_model_calls": run_row.get("used_model_calls"),
        "used_tool_calls": run_row.get("used_tool_calls"),
        "model_mode": mode,
        "token_usage": None,
        "cost": None,
        "usage_unknown": True,
        "note": "token/费用未知或未测量，不显示为 0",
    }


@router.get("/status")
def console_status(_: dict = Depends(_require_admin)) -> dict:
    state = _console_state()
    return {
        "console": state["console"], "reason": state["reason"],
        "roots": state["roots"],
        "real_link_verified": False,
        "effect_verified": False,
        "note": "演化/效果仍未验证；本页面仅只读展示真实持久记录。",
    }


@router.get("/experiments")
def list_experiments(
    root: str = Query(...),
    q: str = "",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _: dict = Depends(_require_admin),
) -> dict:
    state = _console_state()
    _state_guard(state)
    roots = _roots()
    if root not in roots:
        raise HTTPException(status_code=404, detail="未知实验根标识")
    conn, err = _readonly_db(roots[root])
    if conn is None:
        raise HTTPException(status_code=503, detail=err)
    try:
        where = ""
        params: tuple = ()
        if q:
            where = "WHERE experiment_id LIKE ? OR dataset_version LIKE ?"
            params = (f"%{q}%", f"%{q}%")
        total = _rows(conn, f"SELECT COUNT(*) AS c FROM evolution_experiments "
                            f"{where}", params)[0]["c"]
        rows = _rows(conn, f"SELECT * FROM evolution_experiments {where} "
                           f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
                     params + (size, (page - 1) * size))
        return {"items": [_pick(r, _EXP_KEYS) for r in rows],
                "page": page, "size": size, "total": total}
    finally:
        conn.close()


@router.get("/experiments/{experiment_id}")
def experiment_detail(experiment_id: str, root: str = Query(...),
                      _: dict = Depends(_require_admin)) -> dict:
    roots = _roots()
    state = _console_state()
    _state_guard(state)
    if root not in roots:
        raise HTTPException(status_code=404, detail="未知实验根标识")
    conn, err = _readonly_db(roots[root])
    if conn is None:
        raise HTTPException(status_code=503, detail=err)
    try:
        exp = _rows(conn, "SELECT * FROM evolution_experiments "
                          "WHERE experiment_id=?", (experiment_id,))
        if not exp:
            raise HTTPException(status_code=404, detail="实验不存在")
        exp = exp[0]
        runs = _rows(conn, "SELECT * FROM evolution_runs WHERE experiment_id=? "
                           "ORDER BY created_at", (experiment_id,))
        run_ids = [r["run_id"] for r in runs]
        iters = []
        if run_ids:
            ph = ",".join("?" * len(run_ids))
            iters = _rows(conn, f"SELECT * FROM evolution_iterations "
                                f"WHERE run_id IN ({ph})", tuple(run_ids))
        evals = _rows(conn, "SELECT * FROM evolution_evaluations "
                            "WHERE experiment_id=?", (experiment_id,))
        return {
            "experiment": _pick(exp, _EXP_KEYS),
            "current_skill_set": sanitize_set(
                exp.get("current_skill_set_json") or "{}"),
            "best_score": ({"passed": exp.get("best_score_passed"),
                            "total": exp.get("best_score_total")}
                           if exp.get("best_score_total") is not None else None),
            "runs_summary": [{**_pick(r, ("run_id", "status", "stop_reason",
                                          "current_iteration",
                                          "max_iterations", "created_at",
                                          "finished_at")),
                              "model_mode": _budget_summary(r)["model_mode"],
                              "used_model_calls": r.get("used_model_calls"),
                              "model_cap": _budget_summary(r)["model_cap"],
                              "usage_unknown": True,
                              "cost": None}
                             for r in runs],
            "iterations_total": len(iters),
            "evaluations_total": len(evals),
        }
    finally:
        conn.close()


@router.get("/runs/{run_id}")
def run_detail(run_id: str, root: str = Query(...),
               _: dict = Depends(_require_admin)) -> dict:
    roots = _roots()
    _state_guard(_console_state())
    if root not in roots:
        raise HTTPException(status_code=404, detail="未知实验根标识")
    conn, err = _readonly_db(roots[root])
    if conn is None:
        raise HTTPException(status_code=503, detail=err)
    try:
        runs = _rows(conn, "SELECT * FROM evolution_runs WHERE run_id=?",
                     (run_id,))
        if not runs:
            raise HTTPException(status_code=404, detail="运行不存在")
        run = runs[0]
        iters = _rows(conn, "SELECT * FROM evolution_iterations WHERE run_id=? "
                            "ORDER BY number", (run_id,))
        return {
            "run": _pick(run, _RUN_KEYS),
            "budget": _budget_summary(run),
            "iterations": [_pick(i, _ITER_KEYS) for i in iters],
        }
    finally:
        conn.close()


@router.get("/experiments/{experiment_id}/skills")
def experiment_skills(experiment_id: str, root: str = Query(...),
                      _: dict = Depends(_require_admin)) -> dict:
    roots = _roots()
    _state_guard(_console_state())
    if root not in roots:
        raise HTTPException(status_code=404, detail="未知实验根标识")
    conn, err = _readonly_db(roots[root])
    if conn is None:
        raise HTTPException(status_code=503, detail=err)
    try:
        exp = _rows(conn, "SELECT current_skill_set_json FROM "
                          "evolution_experiments WHERE experiment_id=?",
                    (experiment_id,))
        if not exp:
            raise HTTPException(status_code=404, detail="实验不存在")
        set_json = sanitize_set(exp[0]["current_skill_set_json"] or "{}")
        members = set_json["members"]
        versions = []
        for m in members:
            vs = _rows(conn, "SELECT * FROM evolution_skill_versions "
                             "WHERE version_id=?", (m.get("version_id"),))
            for v in vs:
                versions.append({
                    **_pick(v, _SKILL_KEYS),
                    "skill_md_chars": len(v.get("skill_md") or ""),
                    "purpose_md_chars": len(v.get("purpose_md") or ""),
                    "content_immutable": True,
                })
        run_ids = [r["run_id"] for r in _rows(
            conn, "SELECT run_id FROM evolution_runs WHERE experiment_id=?",
            (experiment_id,))]
        proposals: list[dict] = []
        if run_ids:
            ph = ",".join("?" * len(run_ids))
            for p in _rows(conn, f"SELECT * FROM evolution_proposals "
                                 f"WHERE run_id IN ({ph}) ORDER BY created_at",
                           tuple(run_ids)):
                proposals.append({
                    **_pick(p, _PROP_KEYS),
                    "reason_chars": len(p.get("reason") or ""),
                    "patch_chars": len(p.get("patch_json") or ""),
                    "diff_summary": (f"parent {p.get('parent_version_id')} -> "
                                     f"candidate {p.get('candidate_version_id')}")
                                    if p.get("action") != "no_action" else "no_action",
                })
        return {"current_members": members, "versions": versions,
                "candidates": proposals,
                "content_diff_supported": True,
                "note": "正文差异经 GET /skills/{version_id}/diff 按精确版本查看；"
                        "候选条目在此仅展示版本/哈希与 diff 元数据"}
    finally:
        conn.close()


@router.get("/experiments/{experiment_id}/versions")
def experiment_versions_catalog(experiment_id: str, root: str = Query(...),
                                _: dict = Depends(_require_admin)) -> dict:
    """该实验根下的全部不可变技能版本元数据（供正文/差异按精确版本选择）。

    只透出白名单元数据字段，不含正文/哈希外内容；正文经 /skills/{id}/content。
    """
    roots = _roots()
    _state_guard(_console_state())
    if root not in roots:
        raise HTTPException(status_code=404, detail="未知实验根标识")
    conn, err = _readonly_db(roots[root])
    if conn is None:
        raise HTTPException(status_code=503, detail=err)
    try:
        exp = _rows(conn, "SELECT experiment_id FROM evolution_experiments "
                          "WHERE experiment_id=?", (experiment_id,))
        if not exp:
            raise HTTPException(status_code=404, detail="实验不存在")
        rows = _rows(conn, "SELECT * FROM evolution_skill_versions "
                           "ORDER BY skill_id, seq")
        versions = [{
            **_pick(v, _SKILL_KEYS),
            "skill_md_chars": len(v.get("skill_md") or ""),
            "purpose_md_chars": len(v.get("purpose_md") or ""),
        } for v in rows]
        return {"items": versions, "total": len(versions)}
    finally:
        conn.close()


@router.get("/experiments/{experiment_id}/patterns")
def experiment_patterns(experiment_id: str, root: str = Query(...),
                        page: int = Query(1, ge=1),
                        size: int = Query(20, ge=1, le=100),
                        _: dict = Depends(_require_admin)) -> dict:
    roots = _roots()
    _state_guard(_console_state())
    if root not in roots:
        raise HTTPException(status_code=404, detail="未知实验根标识")
    conn, err = _readonly_db(roots[root])
    if conn is None:
        raise HTTPException(status_code=503, detail=err)
    try:
        exp = _rows(conn, "SELECT workspace_id,domain FROM "
                          "evolution_experiments WHERE experiment_id=?",
                    (experiment_id,))
        if not exp:
            raise HTTPException(status_code=404, detail="实验不存在")
        ws, domain = exp[0]["workspace_id"], exp[0]["domain"]
        rows = _rows(conn, "SELECT * FROM evolution_patterns WHERE "
                           "workspace_id=? AND domain=? ORDER BY created_at DESC "
                           "LIMIT ? OFFSET ?",
                     (ws, domain, size, (page - 1) * size))
        items = []
        for p in rows:
            pid = p["pattern_id"]
            rev = _rows(conn, "SELECT COUNT(*) AS c FROM "
                              "evolution_pattern_revisions WHERE pattern_id=?",
                        (pid,))[0]["c"]
            log = _rows(conn, "SELECT COUNT(*) AS c FROM evolution_logs "
                              "WHERE workspace_id=? AND domain=?",
                        (ws, domain))[0]["c"]
            items.append({
                **_pick(p, ("pattern_id", "status", "title",
                            "current_revision_id", "created_at")),
                "revision_count": rev, "log_count": log,
            })
        return {"items": items, "page": page, "size": size,
                "total": len(items)}
    finally:
        conn.close()


@router.get("/experiments/{experiment_id}/gate-history")
def experiment_gate_history(experiment_id: str, root: str = Query(...),
                            page: int = Query(1, ge=1),
                            size: int = Query(20, ge=1, le=100),
                            _: dict = Depends(_require_admin)) -> dict:
    roots = _roots()
    _state_guard(_console_state())
    if root not in roots:
        raise HTTPException(status_code=404, detail="未知实验根标识")
    conn, err = _readonly_db(roots[root])
    if conn is None:
        raise HTTPException(status_code=503, detail=err)
    try:
        total = _rows(conn, "SELECT COUNT(*) AS c FROM evolution_gate_events "
                            "WHERE experiment_id=?", (experiment_id,))[0]["c"]
        rows = _rows(conn, "SELECT * FROM evolution_gate_events WHERE "
                           "experiment_id=? ORDER BY created_at DESC "
                           "LIMIT ? OFFSET ?",
                     (experiment_id, size, (page - 1) * size))
        items = []
        for g in rows:
            score = None
            raw = g.get("candidate_score")
            if raw:
                try:
                    score = json.loads(raw)
                except ValueError:
                    score = {"unparsed": True}
            items.append({
                **_pick(g, ("event_id", "decision", "reason", "created_at",
                            "candidate_version_ids_json")),
                "candidate_score": score,
            })
        return {"items": items, "page": page, "size": size, "total": total}
    finally:
        conn.close()


@router.get("/experiments/{experiment_id}/evaluations")
def experiment_evaluations(experiment_id: str, root: str = Query(...),
                           page: int = Query(1, ge=1),
                           size: int = Query(20, ge=1, le=100),
                           _: dict = Depends(_require_admin)) -> dict:
    roots = _roots()
    _state_guard(_console_state())
    if root not in roots:
        raise HTTPException(status_code=404, detail="未知实验根标识")
    conn, err = _readonly_db(roots[root])
    if conn is None:
        raise HTTPException(status_code=503, detail=err)
    try:
        total = _rows(conn, "SELECT COUNT(*) AS c FROM evolution_evaluations "
                            "WHERE experiment_id=?", (experiment_id,))[0]["c"]
        rows = _rows(conn, "SELECT * FROM evolution_evaluations WHERE "
                           "experiment_id=? ORDER BY created_at DESC "
                           "LIMIT ? OFFSET ?",
                     (experiment_id, size, (page - 1) * size))
        items = []
        for r in rows:
            per_task = []
            raw = r.get("per_task_results_json")
            if isinstance(raw, str):
                try:
                    per_task = json.loads(raw)
                except (ValueError, TypeError):
                    per_task = []
            pending = [t.get("task_id") for t in per_task
                       if t.get("verdict") in ("needs_review", "invalid")]
            usage = {}
            uraw = r.get("usage_json")
            if isinstance(uraw, str):
                try:
                    usage = json.loads(uraw)
                except (ValueError, TypeError):
                    usage = {}
            items.append({
                **_pick(r, _EVAL_KEYS),
                "score": f"{r.get('main_passed')}/{r.get('main_total')}"
                         if r.get("valid") else None,
                "pending_tasks": pending,
                "pending_count": len(pending),
                "review_requests": int(usage.get("review_requests") or 0),
            })
        return {"items": items,
                "page": page, "size": size, "total": total}
    finally:
        conn.close()


@router.get("/runs/{run_id}/trajectories")
def run_trajectories(run_id: str, root: str = Query(...),
                     page: int = Query(1, ge=1),
                     size: int = Query(20, ge=1, le=100),
                     _: dict = Depends(_require_admin)) -> dict:
    """轨迹摘要：只读 run 目录 meta（封存摘要），不读取正文内容。"""
    roots = _roots()
    _state_guard(_console_state())
    if root not in roots:
        raise HTTPException(status_code=404, detail="未知实验根标识")
    conn, err = _readonly_db(roots[root])
    if conn is None:
        raise HTTPException(status_code=503, detail=err)
    # 允许的执行 id = 该 run 迭代的训练执行 + 其实验评估逐任务执行
    allowed: set[str] = set()
    iters = _rows(conn, "SELECT train_execution_ids_json FROM "
                        "evolution_iterations WHERE run_id=?", (run_id,))
    for it in iters:
        try:
            raw = json.loads(it["train_execution_ids_json"] or "[]")
        except ValueError:
            continue
        if isinstance(raw, dict):
            for task in raw.get("tasks") or []:
                if isinstance(task, dict) and task.get("execution_id"):
                    allowed.add(task["execution_id"])
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    allowed.add(item)
                elif isinstance(item, dict) and item.get("execution_id"):
                    allowed.add(item["execution_id"])
    run_meta = _rows(conn, "SELECT experiment_id FROM evolution_runs "
                           "WHERE run_id=?", (run_id,))
    if run_meta:
        evals = _rows(conn, "SELECT per_task_results_json FROM "
                            "evolution_evaluations WHERE experiment_id=?",
                      (run_meta[0]["experiment_id"],))
        for e in evals:
            try:
                for t in json.loads(e["per_task_results_json"] or "[]"):
                    if isinstance(t, dict) and t.get("execution_id"):
                        allowed.add(t["execution_id"])
            except ValueError:
                pass
    conn.close()
    runs_dir = roots[root] / _RUNS_REL
    runs_resolved = runs_dir.resolve()
    metas: list[dict] = []
    if runs_dir.is_dir():
        for d in sorted(runs_dir.iterdir()):
            name = d.name
            # 只读边界：名字白名单 + 仅真实目录 + 解析后必须仍在配置根内
            if not name or len(name) > 64:
                continue
            if any(ch not in "0123456789abcdefghijklmnopqrstuvwxyz"
                   "ABCDEFGHIJKLMNOPQRSTUVWXYZ_-" for ch in name):
                continue
            if d.is_symlink() or not d.is_dir():
                continue
            try:
                if not d.resolve().is_relative_to(runs_resolved):
                    continue
            except (OSError, ValueError):
                continue
            if name not in allowed:
                continue
            meta_file = d / "meta.json"
            if not meta_file.is_file() or meta_file.is_symlink():
                continue
            try:
                m = json.loads(meta_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            outcome = m.get("outcome") or {}
            candidate = m.get("candidate") or {}
            # 显式字段白名单：绝不透传 meta/candidate 的正文或其它键
            metas.append({
                "execution_id": d.name,
                "dataset_version": m.get("dataset_version"),
                "domain": m.get("domain"),
                "split": m.get("split"),
                "group_id": m.get("group_id"),
                "run_status": m.get("run_status"),
                "failure_kind": outcome.get("failure_kind"),
                "published": bool(candidate.get("revision_id")),
                "created_at": m.get("created_at"),
            })
    metas.sort(key=lambda x: x.get("created_at") or "")
    total = len(metas)
    start = (page - 1) * size
    return {"items": metas[start:start + size], "page": page, "size": size,
            "total": total,
            "note": "仅摘要；正文/参考答案/来源不开放下载或展示"}


# ---------------------------------------------------------------------------
# 阶段 8B：不可变技能正文与精确版本差异（只读；正文按不可信文本处理）
# ---------------------------------------------------------------------------

# 单文件展示上限（超长只截断展示，不丢元数据；与入库上限独立）。
_CONTENT_CHAR_LIMIT = 20000
_DIFF_LINE_LIMIT = 500


def _canonical_hash(row: dict) -> str:
    payload = {
        "skill_id": row.get("skill_id"),
        "domain": row.get("domain"),
        "runtime_ref": row.get("runtime_ref"),
        "schema_version": _EVOLUTION_SCHEMA_VERSION,
        "skill_md": row.get("skill_md") or "",
        "purpose_md": row.get("purpose_md") or "",
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fetch_version(conn: sqlite3.Connection, version_id: str) -> dict:
    rows = _rows(conn, "SELECT * FROM evolution_skill_versions "
                       "WHERE version_id=?", (version_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="技能版本不存在")
    row = dict(rows[0])
    expected = row.get("content_hash") or ""
    actual = _canonical_hash(row)
    if not expected or actual != expected:
        raise HTTPException(
            status_code=422,
            detail=("技能版本内容校验失败（哈希与记录不符），版本可能损坏："
                    f"{version_id}"))
    return row


def _slice_content(text: str, label: str) -> dict:
    chars = len(text)
    truncated = chars > _CONTENT_CHAR_LIMIT
    body = text[:_CONTENT_CHAR_LIMIT]
    return {"label": label, "chars": chars, "truncated": truncated,
            "body": body}


@router.get("/skills/{version_id}/content")
def skill_version_content(version_id: str, root: str = Query(...),
                          _: dict = Depends(_require_admin)) -> dict:
    """精确版本的不可变正文（SKILL.md / PURPOSE.md）。

    - 只读取由 root 实验库管理的技能版本记录；content 与哈希一致性校验，
      损坏 → 422 明确提示；不存在 → 404；超长截断并标注 truncated。
    - 正文作为不可信文本返回，绝不在此渲染/执行；不做任意文件读取。
    """
    roots = _roots()
    _state_guard(_console_state())
    if root not in roots:
        raise HTTPException(status_code=404, detail="未知实验根标识")
    conn, err = _readonly_db(roots[root])
    if conn is None:
        raise HTTPException(status_code=503, detail=err)
    try:
        row = _fetch_version(conn, version_id)
        return {
            "version_id": row["version_id"],
            "skill_id": row["skill_id"],
            "seq": row["seq"],
            "parent_version_id": row.get("parent_version_id"),
            "content_hash": row["content_hash"],
            "source_type": row["source_type"],
            "integrity": "ok",
            "origin": None,
            "skill_md": _slice_content(row.get("skill_md") or "", "SKILL.md"),
            "purpose_md": _slice_content(row.get("purpose_md") or "",
                                         "PURPOSE.md"),
            "unsafe": True,
        }
    finally:
        conn.close()


@router.get("/skills/{version_id}/diff")
def skill_version_diff(version_id: str, base_version_id: str = Query(...),
                       root: str = Query(...),
                       _: dict = Depends(_require_admin)) -> dict:
    """两个精确不可变版本之间的正文差异（unified diff，SKILL.md + PURPOSE.md）。

    - base_version_id 为旧版本、version_id 为新版本；二者必须都存在且完整。
    - 结果以文本形式返回并截断到 _DIFF_LINE_LIMIT；超长时 truncated=True。
    - 正文/差异都按不可信文本展示，不执行 HTML/指令。
    """
    roots = _roots()
    _state_guard(_console_state())
    if root not in roots:
        raise HTTPException(status_code=404, detail="未知实验根标识")
    conn, err = _readonly_db(roots[root])
    if conn is None:
        raise HTTPException(status_code=503, detail=err)
    try:
        base = _fetch_version(conn, base_version_id)
        head = _fetch_version(conn, version_id)
        if head.get("skill_id") != base.get("skill_id"):
            raise HTTPException(status_code=422,
                                detail="差异仅限同一 skill_id 的精确版本")
        files: list[dict] = []
        for key, label in (("skill_md", "SKILL.md"),
                           ("purpose_md", "PURPOSE.md")):
            a = (base.get(key) or "").splitlines()
            b = (head.get(key) or "").splitlines()
            diff_lines = list(difflib.unified_diff(
                a, b, fromfile=f"{base_version_id}:{label}",
                tofile=f"{version_id}:{label}", lineterm=""))
            truncated = len(diff_lines) > _DIFF_LINE_LIMIT
            body = "\n".join(diff_lines[:_DIFF_LINE_LIMIT])
            files.append({"label": label, "lines": len(diff_lines),
                          "truncated": truncated, "body": body})
        return {
            "base_version_id": base_version_id,
            "version_id": version_id,
            "skill_id": head["skill_id"],
            "files": files,
            "same_content": head["content_hash"] == base["content_hash"],
            "unsafe": True,
        }
    finally:
        conn.close()
