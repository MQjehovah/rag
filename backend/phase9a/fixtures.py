# -*- coding: utf-8 -*-
"""Phase 9A 冻结 fixture 与确定性模型替身（phase9a 契约 §2/§3/§4.1）。

本模块被 seed.py / server.py / tests/test_phase9a_integration.py 复用：
- §3 全部冻结标识：身份（is_local 用户/组/角色）、workspace/notebook/page/
  wiki/evidence 的 id 与内容构造；
- §2 确定性 LLM/graph recorder（同 server 与集成测试同一套行为）；
- §4.1 OpenAPI spec 读取/构造 helper 与自校验。

约束：不含真实模型客户端、不含业务库连接、不含凭据明文（密码仅测试密码）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# 仓库 backend 目录（本文件位于 <repo>/backend/phase9a/fixtures.py）。
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(BACKEND_DIR))

# ---------------------------------------------------------------------------
# 冻结常量（CONTRACT §3）
# ---------------------------------------------------------------------------

PHASE9A_PASSWORD = "Phase9a!2026"

# 注入/检测标记（CONTRACT §2）。
MARKER_FAIL = "PHASE9A_FAIL_MARKER"
MARKER_BLOCK = "PHASE9A_BLOCK_MARKER"

# 身份（全部 is_local=True；组/角色冻结）。
ADMIN_USERNAME = "phase9a-admin"
EDITOR_USERNAME = "phase9a-editor"
READER_USERNAME = "phase9a-reader"

USERS = {
    ADMIN_USERNAME: ["__local_admin__"],
    EDITOR_USERNAME: ["eng", "editors"],
    READER_USERNAME: ["eng"],
}

# Workspace / Notebook / Page / Evidence / Wiki 冻结 id 与关键名。
WS_ENG_ID = "ws-eng"
WS_SALES_ID = "ws-sales"
WS_ENG_KEY = "phase9a-eng"
WS_SALES_KEY = "phase9a-sales"

NB_ENG_ID = "nb-eng"
NB_HIDDEN_ID = "nb-hidden"
NB_SALES_ID = "nb-sales"

PAGE_DEFAULT_ID = "p-default"
PAGE_API_ID = "p-api"
PAGE_HIDDEN_ID = "p-hidden"
PAGE_FAIL_ID = "p-fail"
PAGE_SALES_ID = "p-sales"

EVIDENCE_API_ID = "ev-api"
EVIDENCE_HIDDEN_ID = "ev-hidden"

WIKI_DEFAULT_ID = "w-eng-default"
WIKI_API_ID = "w-eng-api"
WIKI_SALES_ID = "w-sales-default"
WIKI_FAIL_ID = "w-fail"

# 需要真实编译成功的 wiki 目标（w-fail 首轮预期失败）。
WIKI_OK_IDS = (WIKI_DEFAULT_ID, WIKI_API_ID, WIKI_SALES_ID)

# 图谱目标的关键词断言（需在 OpenAPI overview 文本中出现：两个实体 + 关系词，
# 使真实图谱建图后能产生实体/关系行）。
GRAPH_ENTITY_WORD = "上位机"
GRAPH_ENTITY_WORD2 = "调试器"
GRAPH_RELATION_WORD = "支持"

# OpenAPI fixture 文件（§4.1 提交文件）。
USERS_API_FIXTURE_PATH = (
    BACKEND_DIR / "tests" / "fixtures" / "api_reference" / "phase9a_users_api.json"
)


def sha256_hex(text: str) -> str:
    """确定性 sha256 hexdigest（64 小写 hex，evidence 校验要求）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def spec_json_dumps(spec: dict) -> str:
    """把 OpenAPI dict 序列化为 UTF-8 紧凑 JSON 文本。"""
    return json.dumps(spec, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# §4.1 users_api OpenAPI fixture（dict 构造 + 自校验；JSON 文件内容同源）
# ---------------------------------------------------------------------------


def users_openapi_spec() -> dict:
    """phase9a_users_api.json 的内存构造（v1/v2 双版本端点 + 参数 + media + 错误码）。

    实体/关系词嵌在 info.description（会进入 published Overview Section，
    供真实图谱建图断言），两文档描述不同但都含相同图谱短语，合并后 canonical
    内容必然保留短语（p-api 字典序在 p-hidden 之前）。
    """
    overview = (
        "Phase9A 用户接口。上位机系统承载全部接口，支持连接调试器诊断调用。"
        "返回 200 成功，401/403 认证失败，404 目标不存在。"
    )
    return {
        "openapi": "3.0.1",
        "info": {
            "title": "Phase9A 用户接口",
            "version": "1.0",
            "description": overview,
        },
        "paths": {
            "/v1/users": {
                "get": {
                    "summary": "查询用户列表（v1）",
                    "description": "分页返回用户列表。支持 limit/offset 分页。",
                    "parameters": [
                        {"name": "limit", "in": "query", "required": False,
                         "description": "每页条数", "schema": {"type": "integer",
                                                              "default": 50}},
                        {"name": "offset", "in": "query", "required": False,
                         "description": "偏移量", "schema": {"type": "integer",
                                                            "default": 0}},
                    ],
                    "responses": {
                        "200": {
                            "description": "用户列表",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "array",
                                               "items": {"$ref": "#/components/schemas/User"}}
                                }
                            },
                        },
                        "400": {"description": "请求参数不合法",
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Error"}
                                    }
                                }},
                        "401": {"description": "未认证"},
                        "403": {"description": "无权限"},
                        "404": {"description": "接口不存在"},
                        "500": {"description": "服务器内部错误"},
                    },
                }
            },
            "/v1/users/{id}": {
                "get": {
                    "summary": "查询单个用户（v1）",
                    "description": "按 id 获取用户详情。",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True,
                         "description": "用户 id", "schema": {"type": "string"}},
                    ],
                    "responses": {
                        "200": {
                            "description": "用户详情",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/User"}
                                }
                            },
                        },
                        "404": {"description": "用户不存在",
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Error"}
                                    }
                                }},
                        "500": {"description": "服务器内部错误"},
                    },
                }
            },
            "/v2/users": {
                "get": {
                    "summary": "查询用户列表（v2）",
                    "description": "v2 用户列表，分页返回。",
                    "parameters": [
                        {"name": "limit", "in": "query", "required": False,
                         "description": "每页条数", "schema": {"type": "integer",
                                                              "default": 50}},
                    ],
                    "responses": {
                        "200": {
                            "description": "用户列表",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "array",
                                               "items": {"$ref": "#/components/schemas/User"}}
                                }
                            },
                        },
                        "401": {"description": "未认证"},
                        "403": {"description": "无权限"},
                    },
                }
            },
            "/v2/users/{id}": {
                "get": {
                    "summary": "查询单个用户（v2）",
                    "description": "v2 用户详情。",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True,
                         "description": "用户 id", "schema": {"type": "string"}},
                    ],
                    "responses": {
                        "200": {
                            "description": "用户详情",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/User"}
                                }
                            },
                        },
                        "404": {"description": "用户不存在"},
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "User": {
                    "type": "object",
                    "description": "用户对象",
                    "required": ["id", "name"],
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "email": {"type": "string"},
                    },
                },
                "Error": {
                    "type": "object",
                    "description": "错误响应体",
                    "required": ["code", "message"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                    },
                },
            }
        },
    }


def hidden_openapi_spec() -> dict:
    """p-hidden 的内联 OpenAPI（单端点 GET /v2/audit）。"""
    return {
        "openapi": "3.0.1",
        "info": {
            "title": "Phase9A 审计接口",
            "version": "1.0",
            "description": (
                "Phase9A 审计接口。上位机系统承载该接口，支持连接调试器获取操作审计。"
                "返回 200 成功，401 未认证，403 无权限。"
            ),
        },
        "paths": {
            "/v2/audit": {
                "get": {
                    "summary": "查询审计记录",
                    "description": "返回最近审计记录。",
                    "parameters": [
                        {"name": "limit", "in": "query", "required": False,
                         "description": "条数", "schema": {"type": "integer",
                                                          "default": 20}},
                    ],
                    "responses": {
                        "200": {
                            "description": "审计记录",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "array",
                                               "items": {"type": "string"}}
                                }
                            },
                        },
                        "401": {"description": "未认证"},
                        "403": {"description": "无权限"},
                    },
                }
            }
        },
    }


def users_api_text() -> str:
    """读取提交的 phase9a_users_api.json 并校验（seed 的 p-api 内容与断言共用）。"""
    if not USERS_API_FIXTURE_PATH.exists():
        raise FileNotFoundError(f"missing openapi fixture: {USERS_API_FIXTURE_PATH}")
    text = USERS_API_FIXTURE_PATH.read_text(encoding="utf-8")
    spec = json.loads(text)
    _assert_users_api_covers(spec)
    return text


def _assert_users_api_covers(spec: dict) -> None:
    """自校验：至少含 /v1/users 与 /v2/users GET、query limit、path id、media、≥400 错误码。"""
    paths = spec.get("paths") or {}
    assert isinstance(paths.get("/v1/users"), dict) and "get" in paths["/v1/users"]
    assert isinstance(paths.get("/v2/users"), dict) and "get" in paths["/v2/users"]
    v1 = paths["/v1/users"]["get"]
    params = v1.get("parameters") or []
    assert any(p.get("name") == "limit" and p.get("in") == "query" for p in params)
    v1_item = paths.get("/v1/users/{id}")
    assert isinstance(v1_item, dict) and "get" in v1_item
    id_params = v1_item["get"].get("parameters") or []
    assert any(p.get("name") == "id" and p.get("in") == "path" for p in id_params)
    responses = v1.get("responses") or {}
    assert "200" in responses
    content = responses["200"].get("content") or {}
    assert "application/json" in content
    codes = {c for c in responses if c.isdigit() and int(c) >= 400}
    assert codes, "spec must declare >=400 business error codes"
    assert spec.get("components", {}).get("schemas"), "spec must declare components/schemas"
    # 覆盖断言所需的两版本 section_key 形状（路径含 /v1、/v2）。
    assert "get" in paths["/v2/users"]
    # 图谱短语必须出现，保证真实图谱建图可产生实体/关系行。
    info_desc = (spec.get("info") or {}).get("description") or ""
    assert GRAPH_ENTITY_WORD in info_desc and GRAPH_ENTITY_WORD2 in info_desc
    assert GRAPH_RELATION_WORD in info_desc


def hidden_api_text() -> str:
    """p-hidden 单端点 OpenAPI JSON 文本。"""
    return spec_json_dumps(hidden_openapi_spec())


# ---------------------------------------------------------------------------
# 纯中文 long 文本构造（§3.2；不含语义版本号/数字点）
# ---------------------------------------------------------------------------


def _zh_text(seed_para: str, min_chars: int) -> str:
    """按段落重复到至少 min_chars 的确定性中文文本（不含半角点/数字点）。"""
    parts: list[str] = []
    total = 0
    while total < min_chars:
        parts.append(seed_para)
        total += len(seed_para)
    text = "".join(parts)
    return text[: min_chars * 3]


def page_default_text() -> str:
    """p-default 纯中文长文：Phase9A 编译使用说明（不含语义版本号/数字点）。"""
    para = (
        "Phase9A 编译使用说明。本系统把原始文档编译为结构化的企业知识条目，"
        "编译过程会先抽取页面要点，再生成聚合正文并发布到知识库。"
        "使用时请先完成工作区绑定，再把资料放入对应笔记本，随后触发脏数据刷新。"
    )
    text = _zh_text(para, 400)
    # 排除 ASCII 半角点与「数字点」形态（Phase9A 是冻结产品名，允许其自身数字）。
    assert "." not in text
    assert not re.search(r"\d+\.\d+", text)
    return text


def page_sales_text() -> str:
    """p-sales 纯中文长文：Phase9A 销售报价流程。"""
    para = (
        "Phase9A 销售报价流程。销售同事收到客户询价后，先确认产品型号与数量，"
        "再查询历史成交价并填写报价单，最后由主管审批后发给客户。"
    )
    return _zh_text(para, 400)


def page_fail_text() -> str:
    """p-fail：含故障注入标记的长文本（标记放最前，保证进入 LLM prompt）。"""
    para = (
        "Phase9A 故障主题。本页用于验证编译失败与重试语义，"
        "内容包含特殊标记以模拟外部模型故障。"
    )
    return MARKER_FAIL + "\n" + _zh_text(para, 400)


def page_block_text() -> str:
    """p-block：含阻塞标记的长文本（cancel 语义专用，fixture 内联）。"""
    para = (
        "Phase9A 阻塞主题。本页用于验证运行中取消语义，"
        "内容包含阻塞标记以模拟长时模型调用。"
    )
    return MARKER_BLOCK + "\n" + _zh_text(para, 400)


# ---------------------------------------------------------------------------
# 确定性 Embedding（与 backend/tests/conftest.py 同语义，server/测试复用）
# ---------------------------------------------------------------------------


def fake_embedding_vector(text: str, dimensions: int) -> list[float]:
    """确定性伪向量：sha256(text) 派生，同文本同向量。"""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    return [((seed[i % len(seed)] / 255.0) * 2.0) - 1.0 for i in range(dimensions)]


def patch_embedding_fake(settings) -> None:
    """类级替换 EmbeddingService.encode/encode_batch（同 conftest 语义）。"""
    from app.core.rag import EmbeddingService

    dims = int(getattr(settings, "embedding_dimensions", 1024))

    async def _fake_encode(self, text: str) -> list[float]:  # noqa: ANN001
        return fake_embedding_vector(text, dims)

    async def _fake_encode_batch(self, texts, batch_size: int = 32) -> list[list[float]]:  # noqa: ANN001
        return [fake_embedding_vector(t, dims) for t in texts]

    EmbeddingService.encode = _fake_encode
    EmbeddingService.encode_batch = _fake_encode_batch


# ---------------------------------------------------------------------------
# §2 LLM / graph recorder（server 与集成测试共用同一套行为）
# ---------------------------------------------------------------------------

_WIKI_SYNTHESIS_ANSWER = {"summary": "Phase9A 摘要", "content": "Phase9A 聚合正文。" + "x" * 60}
_INGEST_ANSWER = {"worthy": True, "ops": []}
_GENERIC_ANSWER = {"worthy": True, "ops": []}


class Recorder:
    """带调用记录的确定性外部 runner 工厂。

    - record_file：JSONL（字段 seq/ts/kind/context/prompt_len/target 白名单，
      绝不记录正文/凭据/路径）；
    - fault_flag：存在＝故障注入/阻塞开关文件（存在与否判定，不以内容为准）；
    - block_timeout：阻塞轮询上限（cancel 语义测试用）。
    """

    def __init__(self, record_file: str | os.PathLike,
                 fault_flag: str | os.PathLike | None = None,
                 block_timeout: float = 60.0) -> None:
        self.record_path = Path(record_file)
        self.fault_flag = Path(fault_flag) if fault_flag else None
        self.block_timeout = float(block_timeout)
        self._lock = threading.Lock()
        self._seq = 0
        self.block_entered = threading.Event()

    # -- 记录 --
    def _record(self, **fields) -> None:
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._seq += 1
            line = {"seq": self._seq, "ts": datetime.now(timezone.utc).isoformat(),
                    **fields}
            with self.record_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    def _flag_present(self) -> bool:
        return bool(self.fault_flag and self.fault_flag.exists())

    # -- LLM runner（CONTRACT §2 行为） --
    def llm_runner(self) -> Callable:
        """返回同步 llm_runner(messages, context='', timeout=120.0) -> dict。"""

        def _llm(messages, context: str = "", timeout: float = 120.0) -> dict:
            first = ""
            if isinstance(messages, (list, tuple)) and messages:
                item = messages[0]
                if isinstance(item, dict):
                    first = str(item.get("content") or "")
            self._record(kind="llm", context=context or "", prompt_len=len(first))
            if self._flag_present():
                if MARKER_FAIL in first:
                    raise RuntimeError("phase9a injected failure")
                if MARKER_BLOCK in first:
                    self.block_entered.set()
                    deadline = time.monotonic() + self.block_timeout
                    while self._flag_present() and time.monotonic() < deadline:
                        time.sleep(1.0)
                    self.block_entered.clear()
            if context == "wiki-synthesis":
                return dict(_WIKI_SYNTHESIS_ANSWER)
            if context == "wiki-ingest-page":
                return dict(_INGEST_ANSWER)
            if context == "api-reference-compile":
                return []
            return dict(_GENERIC_ANSWER)

        return _llm

    # -- graph runner（CONTRACT §2：先记录再委托真实实现） --
    def graph_runner(self, delegate) -> Callable:
        """graph_rec(**kw)：记录 target 白名单后委托真实建图实现，透传返回值。"""

        def _graph(**kw) -> None:
            target = {}
            for key in ("wiki_page_id", "page_id", "remove_page", "remove_wiki"):
                if key in kw:
                    target[key] = kw[key]
            self._record(kind="graph", **target)
            return delegate(**kw)

        return _graph


def load_records(record_file: str | os.PathLike) -> list[dict]:
    """读取 JSONL 记录（集成测试断言用）。"""
    path = Path(record_file)
    if not path.exists():
        return []
    lines = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except (ValueError, TypeError):
                continue
    return lines


# ---------------------------------------------------------------------------
# seed（ORM 直插，幂等；供 seed.py CLI 与集成测试共用）
# ---------------------------------------------------------------------------


def apply_seed(db) -> dict:
    """把 CONTRACT §3 全部数据确定性写入已升级库（幂等，可重复执行）。

    不 commit（由调用方统一提交）。返回冻结摘要（username/wiki/page/evidence/
    workspace id+key）。user_groups 每次执行先清空该用户旧组再重建。
    """
    from app.models.database import (
        EvidenceItem,
        Notebook,
        NotebookWorkspaceBinding,
        Page,
        User,
        UserGroup,
        WikiPage,
        WikiWorkspace,
    )

    # ---- 用户 ----
    pwd_ctx = None
    from passlib.context import CryptContext

    pwd_ctx = CryptContext(schemes=["bcrypt"])
    for username, groups in USERS.items():
        uid = username
        user = db.get(User, uid)
        if user is None:
            user = User(id=uid, username=username, is_local=True,
                        password_hash=pwd_ctx.hash(PHASE9A_PASSWORD),
                        is_active=True)
            db.add(user)
            db.flush()
        else:
            user.username = username
            user.is_local = True
            user.is_active = True
            user.password_hash = pwd_ctx.hash(PHASE9A_PASSWORD)
            db.flush()
        db.query(UserGroup).filter(UserGroup.user_id == uid).delete()
        db.flush()
        for group in groups:
            db.add(UserGroup(id=f"ug-{username}-{group}", user_id=uid,
                             group_name=group))

    # ---- workspaces ----
    ws_rows: dict[str, WikiWorkspace] = {}
    for ws_id, key, name, group in (
        (WS_ENG_ID, WS_ENG_KEY, "Phase9A 工程知识库", "eng"),
        (WS_SALES_ID, WS_SALES_KEY, "Phase9A 销售知识库", "sales"),
    ):
        acl = json.dumps({"groups": [group]}, ensure_ascii=False)
        scope_id = f"group:{group}"
        ws = db.get(WikiWorkspace, ws_id)
        if ws is None:
            ws = WikiWorkspace(id=ws_id, key=key, name=name, acl_scope=acl,
                               scope_id=scope_id, status="active")
            db.add(ws)
            db.flush()
        else:
            ws.key = key
            ws.name = name
            ws.acl_scope = acl
            ws.scope_id = scope_id
            ws.status = "active"
            db.flush()
        ws_rows[ws_id] = ws

    # ---- notebooks ----
    nb_rows: dict[str, Notebook] = {}
    for nb_id, name, group in (
        (NB_ENG_ID, "工程手册", "eng"),
        (NB_HIDDEN_ID, "隐藏来源", "eng2"),
        (NB_SALES_ID, "销售报价", "sales"),
    ):
        nb = db.get(Notebook, nb_id)
        if nb is None:
            nb = Notebook(id=nb_id, name=name, group_id=group)
            db.add(nb)
            db.flush()
        else:
            nb.name = name
            nb.group_id = group
            db.flush()
        nb_rows[nb_id] = nb

    # ---- bindings（nb-hidden 绑定到 ws-eng：ACL 不等价但业务上允许，用于不可见来源）----
    for nb_id, ws_id in ((NB_ENG_ID, WS_ENG_ID), (NB_HIDDEN_ID, WS_ENG_ID),
                         (NB_SALES_ID, WS_SALES_ID)):
        binding = (
            db.query(NotebookWorkspaceBinding)
            .filter(NotebookWorkspaceBinding.notebook_id == nb_id,
                    NotebookWorkspaceBinding.workspace_id == ws_id)
            .first()
        )
        if binding is None:
            binding = NotebookWorkspaceBinding(id=f"b-{nb_id}-{ws_id}",
                                               notebook_id=nb_id,
                                               workspace_id=ws_id,
                                               status="active")
            db.add(binding)
            db.flush()
        else:
            binding.status = "active"
            db.flush()

    # ---- pages 内容 ----
    page_contents = {
        PAGE_DEFAULT_ID: (NB_ENG_ID, "Phase9A 编译使用说明", page_default_text()),
        PAGE_API_ID: (NB_ENG_ID, "Phase9A 用户接口文档", users_api_text()),
        PAGE_HIDDEN_ID: (NB_HIDDEN_ID, "Phase9A 审计接口文档", hidden_api_text()),
        PAGE_FAIL_ID: (NB_ENG_ID, "Phase9A 故障主题", page_fail_text()),
        PAGE_SALES_ID: (NB_SALES_ID, "Phase9A 销售报价流程", page_sales_text()),
    }
    pages: dict[str, Page] = {}
    for pid, (nb_id, title, content) in page_contents.items():
        page = db.get(Page, pid)
        if page is None:
            page = Page(id=pid, notebook_id=nb_id, title=title, content=content,
                        content_hash=sha256_hex(content), wiki_dirty=True)
            db.add(page)
            db.flush()
        else:
            page.notebook_id = nb_id
            page.title = title
            page.content = content
            page.content_hash = sha256_hex(content)
            page.wiki_dirty = True
            db.flush()
        pages[pid] = page

    # ---- evidence（active，source_doc_hash=page.content_hash，locator {"section":"all"}）----
    evidence_spec = {
        EVIDENCE_API_ID: PAGE_API_ID,
        EVIDENCE_HIDDEN_ID: PAGE_HIDDEN_ID,
    }
    for eid, page_id in evidence_spec.items():
        page = pages[page_id]
        content = page.content or ""
        ev = db.get(EvidenceItem, eid)
        if ev is None:
            ev = EvidenceItem(id=eid, source_page_id=page.id, status="active",
                              evidence_type="text",
                              content=content,
                              locator_json=json.dumps({"section": "all"},
                                                      ensure_ascii=False),
                              content_hash=sha256_hex(content),
                              source_doc_hash=page.content_hash or "")
            db.add(ev)
            db.flush()
        else:
            ev.source_page_id = page.id
            ev.status = "active"
            ev.evidence_type = "text"
            ev.content = content
            ev.locator_json = json.dumps({"section": "all"}, ensure_ascii=False)
            ev.content_hash = sha256_hex(content)
            ev.source_doc_hash = page.content_hash or ""
            db.flush()

    # ---- wikis（locked skill；dirty=True；无 current_revision_id）----
    ws_acl = ws_rows[WS_ENG_ID].acl_scope
    ws_sales_acl = ws_rows[WS_SALES_ID].acl_scope
    wiki_spec = [
        (WIKI_DEFAULT_ID, WS_ENG_ID, "Phase9A 系统使用说明", "default", "1",
         [PAGE_DEFAULT_ID], ws_acl),
        (WIKI_API_ID, WS_ENG_ID, "Phase9A 用户接口参考", "api_reference", "1",
         [PAGE_API_ID, PAGE_HIDDEN_ID], ws_acl),
        (WIKI_SALES_ID, WS_SALES_ID, "Phase9A 销售流程", "default", "1",
         [PAGE_SALES_ID], ws_sales_acl),
        (WIKI_FAIL_ID, WS_ENG_ID, "Phase9A 故障重试主题", "default", "1",
         [PAGE_FAIL_ID], ws_acl),
    ]
    for wid, ws_id, title, skill, skill_ver, page_ids, acl in wiki_spec:
        wiki = db.get(WikiPage, wid)
        if wiki is None:
            wiki = WikiPage(id=wid, title=title, summary="", acl_scope=acl,
                            category="资料", status="draft",
                            source_page_ids=json.dumps(page_ids, ensure_ascii=False),
                            dirty=True, workspace_id=ws_id,
                            content_skill=skill, skill_version=skill_ver,
                            skill_locked=True)
            db.add(wiki)
            db.flush()
        else:
            wiki.title = title
            wiki.summary = ""
            wiki.acl_scope = acl
            wiki.category = "资料"
            wiki.status = "draft"
            wiki.source_page_ids = json.dumps(page_ids, ensure_ascii=False)
            wiki.dirty = True
            wiki.workspace_id = ws_id
            wiki.content_skill = skill
            wiki.skill_version = skill_ver
            wiki.skill_locked = True
            wiki.current_revision_id = None
            db.flush()

    return {
        "usernames": sorted(USERS),
        "wiki_ids": [w[0] for w in wiki_spec],
        "page_ids": sorted(page_contents),
        "evidence_ids": sorted(evidence_spec),
        "workspaces": [
            {"id": ws_rows[WS_ENG_ID].id, "key": ws_rows[WS_ENG_ID].key},
            {"id": ws_rows[WS_SALES_ID].id, "key": ws_rows[WS_SALES_ID].key},
        ],
    }
