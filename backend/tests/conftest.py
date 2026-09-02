"""P0-BE-10：外部模型测试隔离（autouse）+ Windows 临时目录修复。

V3 计划 5.2 P0-BE-10「对 Embedding、Reranker、LLM、视觉服务增加可注入
接口和 Fake/Mock」；P0 验收标准「外部模型不可用：测试不失败」。

策略（生产代码零改动）：
- Embedding：类级 patch encode/encode_batch 为确定性 Fake 向量。
  类对象全局共享，覆盖所有 `EmbeddingService()` 实例化点
  （knowledge/pages/chat/search/search_v2/debts/dingtalk_rag_importer）。
  测试内的显式 patch（monkeypatch 后写 / 实例属性）优先于本 fixture。
- Reranker：清空 reranker_api_url，走生产同款降级（全 1.0 分，不外呼）。
- LLM：清空 llm_api_url。守卫在 call_llm_json 内部读 settings，
  一处配置覆盖所有 from-import 持有点（llm_client/classifier/generator 等）。
- 视觉：关闭 pdf_vision_enabled，_analyze_image 走降级返回 None。

Fake 向量确定性：同文本同向量（sha256 种子），维度取 embedding_dimensions。

Windows 临时目录修复：%TEMP%\\pytest-of-<user> 被 ACL/进程占用时，
pytest-asyncio 的 tmp_path fixture 会抛 PermissionError（WinError 5）。
在 conftest 导入阶段（早于任何 fixture 初始化）把 TEMP/TMP 指向项目内
可写目录，保证 tmp_path/tmp_path_factory 全部可用。
"""
from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.core.rag import EmbeddingService

# Windows 临时目录修复：在 pytest 初始化临时目录前重定向到项目内可写目录。
# 必须强制覆盖（而非 setdefault）：系统环境常已设置 TEMP/TMP 指向
# AppData\Local\Temp，setdefault 不覆盖，导致 pytest tmp_path 命中无权限目录
# （PermissionError: WinError 5）。tempfile.tempdir 让 gettempdir() 直接返回
# 项目内目录，是 tmp_path fixture 的最终依据。
#
# 不使用旧的 .pytest_tmp_work（本机已 Access denied 且不可删除），改为每进程
# 唯一可写目录 backend/.pytest_runtime/<pid>-<uuid>。jieba.cache 也落到该目录。
_RUNTIME_ROOT = Path(__file__).resolve().parent.parent / ".pytest_runtime"
_TEMP_DIR = _RUNTIME_ROOT / f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
_TEMP_DIR.mkdir(parents=True, exist_ok=False)
tempfile.tempdir = str(_TEMP_DIR)
os.environ["TEMP"] = str(_TEMP_DIR)
os.environ["TMP"] = str(_TEMP_DIR)
os.environ["JIEBA_CACHE_DIR"] = str(_TEMP_DIR)


def _cleanup_temp_dir() -> None:
    """仅清理本次明确创建的唯一目录，且验证绝对路径位于 .pytest_runtime 内。"""
    root = _RUNTIME_ROOT.resolve()
    target = _TEMP_DIR.resolve()
    if target.parent == root and target.name.startswith(f"{os.getpid()}-"):
        shutil.rmtree(target, ignore_errors=True)


atexit.register(_cleanup_temp_dir)


def fake_embedding_vector(text: str) -> list[float]:
    """确定性伪向量：sha256(text) 派生，同文本同向量。"""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    dim = settings.embedding_dimensions
    return [((seed[i % len(seed)] / 255.0) * 2.0) - 1.0 for i in range(dim)]


async def fake_encode(self, text: str) -> list[float]:
    return fake_embedding_vector(text)


async def fake_encode_batch(self, texts, batch_size: int = 32) -> list[list[float]]:
    return [fake_embedding_vector(t) for t in texts]


@pytest.fixture(autouse=True)
def _isolate_external_models(monkeypatch):
    """所有测试默认隔离外部模型；测试内显式 patch 优先。"""
    monkeypatch.setattr(settings, "llm_api_url", "")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "reranker_api_url", "")
    monkeypatch.setattr(settings, "pdf_vision_enabled", False)
    monkeypatch.setattr(EmbeddingService, "encode", fake_encode)
    monkeypatch.setattr(EmbeddingService, "encode_batch", fake_encode_batch)
