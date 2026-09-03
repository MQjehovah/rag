"""Phase 6 确定性信号提取测试（20.4）。"""
from __future__ import annotations

from app.core.wiki_skills.signals import (
    SIGNAL_TYPE_ORDER,
    extract_signals,
)


def _types(title="", content="", content_kind=""):
    return {s.signal_type for s in extract_signals(title, content, content_kind)}


def test_http_method_signal():
    assert "http_method" in _types(content="调用 GET /api/users 获取列表")


def test_api_path_signal():
    assert "api_path" in _types(content="接口地址 /api/v1/orders 说明如下")


def test_status_code_signal():
    assert "status_code" in _types(content="状态码：404 表示不存在")


def test_error_code_signal():
    assert "error_code" in _types(content="返回 error_code INVALID_PARAM")


def test_request_json_signal():
    assert "request_json" in _types(content="请求参数：{\"name\": \"x\"}")


def test_response_json_signal():
    assert "response_json" in _types(content="返回参数：{\"code\": 0}")


def test_parameter_table_signal():
    assert "parameter_table" in _types(content="字段说明\n名称 | 类型 | 必填")


def test_step_sequence_signal():
    assert "step_sequence" in _types(content="操作步骤：\n1. 打开\n2. 保存")


def test_config_key_value_signal():
    assert "config_key_value" in _types(content="配置项：\ntimeout=30\nretry=3\n")


def test_version_note_signal():
    assert "version_note" in _types(content="版本说明：仅支持 v3.0 及以上")


def test_generic_text_fallback():
    sigs = extract_signals("普通标题", "这是一段普通说明文字。")
    assert "generic_text" in {s.signal_type for s in sigs}


def test_same_input_same_order():
    a = extract_signals("标题", "调用 POST /api/x 请求参数 name status 404")
    b = extract_signals("标题", "调用 POST /api/x 请求参数 name status 404")
    assert [s.to_dict() for s in a] == [s.to_dict() for s in b]
    # 稳定排序遵循 SIGNAL_TYPE_ORDER。
    order = {t: i for i, t in enumerate(SIGNAL_TYPE_ORDER)}
    types = [s.signal_type for s in a]
    assert types == sorted(types, key=lambda t: order[t])


def test_does_not_modify_input():
    content = "调用 GET /api/x"
    title = "标题"
    before_c, before_t = content, title
    extract_signals(title, content)
    assert content == before_c
    assert title == before_t


def test_long_input_bounded():
    huge = "GET /api/x " * 100_000
    sigs = extract_signals("t", huge)
    # 扫描有界：即便超长输入也只产出受限信号集合（不抛、数量 ≤ 上限）。
    assert len(sigs) <= 12
    for s in sigs:
        assert len(s.value) <= 96 or s.value in ("generic_text", "config_key_value")


def test_signal_count_bounded():
    content = (
        "POST /api/a 状态码 400 error_code ERR_1 请求参数 x 返回参数 y 字段说明 z "
        "操作步骤 n 配置项 key=v 版本 2.0 "
    ) * 50
    assert len(extract_signals("t", content)) <= 12


def test_title_source_signal():
    sigs = extract_signals("GET /api/v1 接口规范", "")
    assert any(s.source == "title" for s in sigs)
    assert any(s.signal_type == "api_path" for s in sigs)


def test_empty_input_no_signals():
    assert extract_signals("", "") == ()


def test_no_full_content_stored_in_value():
    long_text = ("调用接口 " * 1000) + "状态码 404"
    for s in extract_signals("t", long_text):
        assert len(s.value) <= 96
