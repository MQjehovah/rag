# -*- coding: utf-8 -*-
"""Phase 9B Agent B：Pipeline 时间配置校验 + helpers 收紧（phase9b/CONTRACT §3）。

覆盖：
1. 默认值保持 300/300/2.0/30.0；
2. 合法短 lease 配置（lease=6/heartbeat=6/poll=0.5/renew=1.0）经 init kwargs 与
   env 字符串注入都通过（“正常环境变量字符串由 Settings 正常解析”）；
3. 反例矩阵：lease/heartbeat 非正（0/-5）、poll/renew 非正与非有限（0/NaN/±Inf）、
   非法字符串（"abc"）→ Settings 构造抛 ValidationError（非法配置在启动配置阶段
   明确失败，绝不静默回退默认）；
4. renew > min(lease, heartbeat)/3 → 构造失败；恰等于边界 → 通过；
5. helpers 收紧：executor._lease_seconds 与 worker.heartbeat 到期长度同源（同一
   函数/字段）；helpers 直接读 settings，不再吞异常回退模块常量 —— 对已由 pydantic
   校验的 settings 读到的必合法；monkeypatch 非法值在读取处抛错，而非静默用 300。

隔离：本文件全部 Settings 构造用 _env_file=None，避免 backend/.env 干扰；autouse
fixture 清掉四枚 env 变量；monkeypatch 改动全部自动还原。
"""
from __future__ import annotations

import math
import os

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.wiki_pipeline import executor

_ENV_KEYS = (
    "WIKI_PIPELINE_LEASE_SECONDS",
    "WIKI_PIPELINE_HEARTBEAT_TIMEOUT_SECONDS",
    "WIKI_PIPELINE_POLL_INTERVAL_SECONDS",
    "WIKI_PIPELINE_LEASE_RENEW_INTERVAL_SECONDS",
)

# 字段真名（env 是等价的 大写下划线 形态）
_FIELD_NAMES = {
    "wiki_pipeline_lease_seconds": "WIKI_PIPELINE_LEASE_SECONDS",
    "wiki_pipeline_heartbeat_timeout_seconds": "WIKI_PIPELINE_HEARTBEAT_TIMEOUT_SECONDS",
    "wiki_pipeline_poll_interval_seconds": "WIKI_PIPELINE_POLL_INTERVAL_SECONDS",
    "wiki_pipeline_lease_renew_interval_seconds": "WIKI_PIPELINE_LEASE_RENEW_INTERVAL_SECONDS",
}


@pytest.fixture(autouse=True)
def _no_pipeline_env(monkeypatch):
    """每个用例前清掉四枚 env，保证 Settings 构造可预期（_env_file=None 已隔离 .env）。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _new(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# 默认值保持 / 合法短配置通过
# ---------------------------------------------------------------------------


def test_defaults_preserved():
    s = _new()
    assert s.wiki_pipeline_lease_seconds == 300
    assert s.wiki_pipeline_heartbeat_timeout_seconds == 300
    assert s.wiki_pipeline_poll_interval_seconds == 2.0
    assert s.wiki_pipeline_lease_renew_interval_seconds == 30.0
    assert isinstance(s.wiki_pipeline_lease_seconds, int)
    assert isinstance(s.wiki_pipeline_poll_interval_seconds, float)


def test_short_lease_config_strings_accepted():
    """正常环境变量字符串形态（"6"/"0.5"）由 Settings 正常解析。"""
    s = _new(
        wiki_pipeline_lease_seconds="6",
        wiki_pipeline_heartbeat_timeout_seconds="6",
        wiki_pipeline_poll_interval_seconds="0.5",
        wiki_pipeline_lease_renew_interval_seconds="1.0",
    )
    assert s.wiki_pipeline_lease_seconds == 6
    assert s.wiki_pipeline_heartbeat_timeout_seconds == 6
    assert s.wiki_pipeline_poll_interval_seconds == 0.5
    assert s.wiki_pipeline_lease_renew_interval_seconds == 1.0


def test_short_lease_env_injection_accepted(monkeypatch):
    """跨进程 env 注入同款形态：settings env 字符串 → 构造成功且类型正确。"""
    for field, value in {
        "wiki_pipeline_lease_seconds": "6",
        "wiki_pipeline_heartbeat_timeout_seconds": "6",
        "wiki_pipeline_poll_interval_seconds": "0.5",
        "wiki_pipeline_lease_renew_interval_seconds": "1.0",
    }.items():
        monkeypatch.setenv(_FIELD_NAMES[field], value)
    s = Settings(_env_file=None)
    assert s.wiki_pipeline_lease_seconds == 6
    assert s.wiki_pipeline_heartbeat_timeout_seconds == 6
    assert s.wiki_pipeline_poll_interval_seconds == 0.5
    assert s.wiki_pipeline_lease_renew_interval_seconds == 1.0


# ---------------------------------------------------------------------------
# 反例矩阵：Settings 构造抛 ValidationError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["wiki_pipeline_lease_seconds", "wiki_pipeline_heartbeat_timeout_seconds"])
@pytest.mark.parametrize("bad", [0, -1, -5])
def test_positive_int_rejects_non_positive(field, bad):
    with pytest.raises(ValidationError):
        _new(**{field: bad})


@pytest.mark.parametrize("field", ["wiki_pipeline_poll_interval_seconds", "wiki_pipeline_lease_renew_interval_seconds"])
@pytest.mark.parametrize("bad", [0, -0.1, float("nan"), float("inf"), float("-inf")])
def test_finite_positive_float_rejects_bad(field, bad):
    with pytest.raises(ValidationError):
        _new(**{field: bad})


@pytest.mark.parametrize("field", list(_FIELD_NAMES))
@pytest.mark.parametrize("bad", ["abc", ""])
def test_invalid_string_rejected(field, bad):
    with pytest.raises(ValidationError):
        _new(**{field: bad})


def test_invalid_env_string_fails_startup_config(monkeypatch):
    """非法 env（如 "abc"/"0"/"nan"）→ Settings 构造抛错 = 启动配置阶段失败。"""
    for value in ("abc", "0", "-5", "nan", "inf"):
        monkeypatch.setenv("WIKI_PIPELINE_LEASE_SECONDS", value)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)
        monkeypatch.delenv("WIKI_PIPELINE_LEASE_SECONDS", raising=False)


# ---------------------------------------------------------------------------
# renew <= min(lease, heartbeat) / 3（模型级交叉校验）
# ---------------------------------------------------------------------------


def test_renew_budget_boundary_ok():
    """恰等于 min(lease, heartbeat)/3 → 通过（默认 30 <= 100、短配置 1.0 <= 2.0）。"""
    _new()  # 默认 300/300/2.0/30.0
    _new(
        wiki_pipeline_lease_seconds=6,
        wiki_pipeline_heartbeat_timeout_seconds=6,
        wiki_pipeline_lease_renew_interval_seconds=2.0,
    )
    _new(
        wiki_pipeline_lease_seconds=300,
        wiki_pipeline_heartbeat_timeout_seconds=300,
        wiki_pipeline_lease_renew_interval_seconds=100.0,
    )
    _new(
        wiki_pipeline_lease_seconds=90,
        wiki_pipeline_heartbeat_timeout_seconds=300,
        wiki_pipeline_lease_renew_interval_seconds=30.0,  # min=90 → 90/3=30
    )


@pytest.mark.parametrize(
    ("lease", "heartbeat", "renew"),
    [
        (6, 6, 2.01),       # > 6/3
        (300, 300, 100.1),  # > 300/3
        (90, 300, 31.0),    # 非对称：min=90 → 90/3=30
        (1, 1, 0.34),       # 极小 budget=1/3
    ],
)
def test_renew_exceeds_budget_rejected(lease, heartbeat, renew):
    with pytest.raises(ValidationError):
        _new(
            wiki_pipeline_lease_seconds=lease,
            wiki_pipeline_heartbeat_timeout_seconds=heartbeat,
            wiki_pipeline_lease_renew_interval_seconds=renew,
        )


# ---------------------------------------------------------------------------
# helpers 收紧：同源读取 + 不回退默认
# ---------------------------------------------------------------------------


def test_claim_and_heartbeat_same_source():
    """worker.heartbeat 与 executor.claim_by_id 读的是同一函数（executor._lease_seconds）。"""
    import app.core.wiki_pipeline.worker as worker_mod  # noqa: PLC0415

    assert worker_mod._lease_seconds is executor._lease_seconds


def test_helpers_read_settings_fields(monkeypatch):
    """默认态下 helpers 直接回读 settings（默认 300/300/2.0/30.0），不回落模块常量。"""
    from app.config import settings  # noqa: PLC0415
    import app.core.wiki_pipeline.worker as worker_mod  # noqa: PLC0415

    assert executor._lease_seconds() == settings.wiki_pipeline_lease_seconds == 300
    assert worker_mod._heartbeat_timeout_default() == 300
    assert worker_mod._poll_interval() == 2.0
    assert worker_mod._renew_interval_default() == 30.0

    monkeypatch.setattr(settings, "wiki_pipeline_lease_seconds", 4)
    monkeypatch.setattr(settings, "wiki_pipeline_heartbeat_timeout_seconds", 1)
    monkeypatch.setattr(settings, "wiki_pipeline_poll_interval_seconds", 0.5)
    monkeypatch.setattr(settings, "wiki_pipeline_lease_renew_interval_seconds", 0.3)
    assert executor._lease_seconds() == 4
    assert worker_mod._heartbeat_timeout_default() == 1
    assert worker_mod._poll_interval() == 0.5
    assert worker_mod._renew_interval_default() == 0.3


def test_helpers_do_not_silently_fallback_on_illegal_values(monkeypatch):
    """helpers 不再吞异常回退默认：非法值要么在读取处抛错，要么原样浮出（绝非静默用 300）。

    真实运行中 settings 构造已由 pydantic 校验，读到的必合法；此处 monkeypatch 注入
    非法值只是证明“不回退”这条路径已删除。
    """
    from app.config import settings  # noqa: PLC0415
    import app.core.wiki_pipeline.worker as worker_mod  # noqa: PLC0415

    # int 字段读非数字字符串 → int(...) 在 helper 内抛 ValueError（不回落 300）。
    monkeypatch.setattr(settings, "wiki_pipeline_lease_seconds", "abc")
    with pytest.raises(ValueError):
        executor._lease_seconds()
    monkeypatch.setattr(settings, "wiki_pipeline_heartbeat_timeout_seconds", "abc")
    with pytest.raises(ValueError):
        worker_mod._heartbeat_timeout_default()

    # 字符串形态但可解析：读取处解析出值本身（不回退模块常量）。
    monkeypatch.setattr(settings, "wiki_pipeline_lease_seconds", "7")
    monkeypatch.setattr(settings, "wiki_pipeline_heartbeat_timeout_seconds", "7")
    assert executor._lease_seconds() == 7
    assert worker_mod._heartbeat_timeout_default() == 7

    # float 字段读非法值 → 抛错 / NaN 原样浮出（不是 2.0 或 30.0）。
    monkeypatch.setattr(settings, "wiki_pipeline_poll_interval_seconds", "abc")
    with pytest.raises(ValueError):
        worker_mod._poll_interval()
    monkeypatch.setattr(settings, "wiki_pipeline_poll_interval_seconds", float("nan"))
    assert math.isnan(worker_mod._poll_interval()), "NaN 不得被静默回落为 2.0"
    monkeypatch.setattr(settings, "wiki_pipeline_lease_renew_interval_seconds", float("inf"))
    assert worker_mod._renew_interval_default() == float("inf"), "inf 不得被静默回落为 30.0"

    # 清空残留环境变量（若模块单例曾带 env），保证不污染后续读取。
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
