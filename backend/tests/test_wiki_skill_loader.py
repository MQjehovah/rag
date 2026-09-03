"""Phase 6 Loader 测试（20.3：安全路径 / YAML 白名单 / schema / default 行为）。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from app.core.wiki_skills import loader
from app.core.wiki_skills.loader import (
    LoaderError,
    SkillDefaultError,
    SkillLoadError,
    load_all,
    load_builtin_default,
    resolve_builtin_root,
)

_REAL_DEFAULT = Path(__file__).resolve().parent.parent / "app/core/wiki_skills/builtin/default"


@pytest.fixture()
def tmp_root(tmp_path, monkeypatch):
    root = tmp_path / "builtin"
    root.mkdir()
    (root / "default").mkdir()
    # 构造真实 default 的完整副本（加载成功所需全部资源，逐文件复制）。
    for fname in ("skill.yaml", "extraction.schema.json", "blueprint.schema.json",
                  "instructions.md", "runtime.py"):
        shutil.copyfile(str(_REAL_DEFAULT / fname), str(root / "default" / fname))
    monkeypatch.setattr(loader, "resolve_builtin_root", lambda: root)
    return root


def _build_skill(root: Path, name: str, yaml_text: str):
    """构造最小 Skill 目录（5 个必需文件；runtime.py 仅占位不被 loader 读取内容）。"""
    d = root / name
    d.mkdir()
    for fname in ("extraction.schema.json", "blueprint.schema.json",
                  "instructions.md", "runtime.py"):
        shutil.copyfile(str(_REAL_DEFAULT / fname), str(d / fname))
    (d / "skill.yaml").write_text(yaml_text, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# 24. builtin/default 真实加载
# ---------------------------------------------------------------------------


def test_builtin_default_loads_from_real_dir():
    d = load_builtin_default()
    assert d.key == "default"
    assert d.version == "1"
    assert d.applicability_signals == ("generic_text",)


def test_load_all_default_present():
    result = load_all()
    assert "default" in result
    assert "1" in result["default"]


# ---------------------------------------------------------------------------
# 非法 YAML / 未知字段 / Python path
# ---------------------------------------------------------------------------


def test_invalid_yaml_rejected(tmp_root):
    _build_skill(tmp_root, "bad", "key: [unclosed\n  - x")
    with pytest.raises(SkillLoadError):
        loader.load_skill(tmp_root / "bad")


def test_unknown_field_rejected(tmp_root):
    yaml_text = (tmp_root / "default" / "skill.yaml").read_text(encoding="utf-8")
    _build_skill(tmp_root, "zskill", yaml_text + "\nunknown_field: 1\n")
    with pytest.raises(SkillLoadError):
        loader.load_skill(tmp_root / "zskill")


def test_python_path_field_rejected(tmp_root):
    for bad_field in ("python_path", "module", "callable", "remote_url", "script"):
        yaml_text = (tmp_root / "default" / "skill.yaml").read_text(encoding="utf-8")
        _build_skill(tmp_root, f"p-{bad_field}", yaml_text + f"\n{bad_field}: X\n")
        with pytest.raises(SkillLoadError):
            loader.load_skill(tmp_root / f"p-{bad_field}")


# ---------------------------------------------------------------------------
# JSON Schema 校验
# ---------------------------------------------------------------------------


def test_invalid_json_schema_rejected(tmp_root):
    (tmp_root / "default" / "extraction.schema.json").write_text("{bad json", encoding="utf-8")
    with pytest.raises(SkillLoadError):
        loader.load_skill(tmp_root / "default")


def test_schema_non_object_rejected(tmp_root):
    (tmp_root / "default" / "extraction.schema.json").write_text('["not", "object"]', encoding="utf-8")
    with pytest.raises(SkillLoadError):
        loader.load_skill(tmp_root / "default")


def test_schema_missing_id_rejected(tmp_root):
    import json

    data = json.loads((tmp_root / "default" / "blueprint.schema.json").read_text(encoding="utf-8"))
    data.pop("$id", None)
    data.pop("id", None)
    (tmp_root / "default" / "blueprint.schema.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    with pytest.raises(SkillLoadError):
        loader.load_skill(tmp_root / "default")


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------


def test_traversal_rejected():
    with pytest.raises(SkillLoadError):
        loader._safe_join(resolve_builtin_root(), "../x")


def test_absolute_path_rejected():
    with pytest.raises(SkillLoadError):
        loader._safe_join(resolve_builtin_root(), "C:/Windows/evil")
    with pytest.raises(SkillLoadError):
        loader._safe_join(resolve_builtin_root(), "/etc/passwd")


def test_symlink_escape_rejected_or_skipped(tmp_path, monkeypatch):
    root = tmp_path / "builtin"
    root.mkdir()
    for fname in ("skill.yaml", "extraction.schema.json", "blueprint.schema.json",
                  "instructions.md", "runtime.py"):
        (root / "default").mkdir(exist_ok=True)
        shutil.copyfile(str(_REAL_DEFAULT / fname), str(root / "default" / fname))
    monkeypatch.setattr(loader, "resolve_builtin_root", lambda: root)
    target = tmp_path / "outside"
    target.mkdir()
    link = root / "default" / "evil_link"
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("当前平台无法创建符号链接（安全跳过）")
    # 指向外部目录的相对链接 realpath 逃逸 → 拒绝。
    with pytest.raises(SkillLoadError):
        loader._safe_join(root, "default/evil_link/secret")


# ---------------------------------------------------------------------------
# Phase 6.1：目录边界 / 必需资源 / runtime.py / schema ID 对齐
# ---------------------------------------------------------------------------


def _default_dir(root: Path) -> Path:
    d = root / "default"
    for fname in ("skill.yaml", "extraction.schema.json", "blueprint.schema.json",
                  "instructions.md", "runtime.py"):
        (d).mkdir(exist_ok=True)
        shutil.copyfile(str(_REAL_DEFAULT / fname), str(d / fname))
    return d


def test_missing_runtime_py_rejected(tmp_root):
    (tmp_root / "default" / "runtime.py").unlink()
    with pytest.raises(SkillLoadError):
        loader.load_skill(tmp_root / "default")


def test_runtime_py_directory_rejected(tmp_root):
    rt = tmp_root / "default" / "runtime.py"
    rt.unlink()
    rt.mkdir()  # runtime.py 是目录而非普通文件
    with pytest.raises(SkillLoadError):
        loader.load_skill(tmp_root / "default")


def test_load_skill_outside_root_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(SkillLoadError):
        loader.load_skill(outside)


def test_load_skill_absolute_external_rejected(tmp_path):
    # tmp_path 是真实 builtin 根外的绝对目录 → 拒绝。
    with pytest.raises(SkillLoadError):
        loader.load_skill(tmp_path)


def test_load_skill_deep_non_direct_child_rejected(tmp_root):
    deep = tmp_root / "default" / "nested"
    deep.mkdir()
    with pytest.raises(SkillLoadError):
        loader.load_skill(deep)


def test_skill_dir_symlink_outside_rejected_or_skipped(tmp_path, monkeypatch):
    root = tmp_path / "builtin2"
    root.mkdir()
    _default_dir(root)
    monkeypatch.setattr(loader, "resolve_builtin_root", lambda: root)
    outside = tmp_path / "ext"
    outside.mkdir()
    link = root / "evilskill"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("当前平台无法创建符号链接（安全跳过）")
    with pytest.raises(SkillLoadError):
        loader.load_skill(link)


def test_extraction_schema_id_mismatch_rejected(tmp_root):
    # 修改 YAML 的 extraction_schema_id 使其与 JSON $id 不一致。
    yaml_path = tmp_root / "default" / "skill.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    yaml_path.write_text(
        text.replace("default-extraction/v1", "default-extraction/v9"),
        encoding="utf-8",
    )
    with pytest.raises(SkillLoadError):
        loader.load_skill(tmp_root / "default")


def test_blueprint_schema_id_mismatch_rejected(tmp_root):
    yaml_path = tmp_root / "default" / "skill.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    yaml_path.write_text(
        text.replace("default-blueprint/v1", "default-blueprint/v9"),
        encoding="utf-8",
    )
    with pytest.raises(SkillLoadError):
        loader.load_skill(tmp_root / "default")


def test_loader_does_not_read_or_execute_runtime_py(tmp_root):
    # runtime.py 写入非法字节内容：若 loader 读取/执行内容会失败；
    # 只做普通文件存在性检查 → 加载成功。
    (tmp_root / "default" / "runtime.py").write_bytes(b"\xff\xfe\x00 not python")
    d = loader.load_skill(tmp_root / "default")
    assert d.key == "default"


# ---------------------------------------------------------------------------
# instructions 超长 / 非 UTF-8
# ---------------------------------------------------------------------------


def test_instructions_too_long_rejected(tmp_root):
    (tmp_root / "default" / "instructions.md").write_text("x" * 300_000, encoding="utf-8")
    with pytest.raises(SkillLoadError):
        loader.load_skill(tmp_root / "default")


def test_non_utf8_declaration_rejected(tmp_root):
    (tmp_root / "default" / "skill.yaml").write_bytes(b"key: default\nversion: \xff\xfe\n")
    with pytest.raises(SkillLoadError):
        loader.load_skill(tmp_root / "default")


# ---------------------------------------------------------------------------
# 非 default 损坏隔离；default 损坏启动失败
# ---------------------------------------------------------------------------


def test_broken_non_default_isolated(tmp_root):
    yaml_text = (tmp_root / "default" / "skill.yaml").read_text(encoding="utf-8")
    bad = _build_skill(tmp_root, "badskill", "key: badskill\nversion: 1\nnot_valid_yaml")
    result = load_all()
    assert "default" in result
    assert "badskill" not in result
    diagnostics = loader.load_diagnostics()
    assert any(d.get("skill") == "badskill" for d in diagnostics)


def test_broken_default_fails_startup(tmp_root):
    (tmp_root / "default" / "skill.yaml").write_text("key: default\nversion: 1\n{{{{", encoding="utf-8")
    with pytest.raises(SkillDefaultError):
        load_all()


def test_missing_default_fails(tmp_root):
    shutil.rmtree(str(tmp_root / "default"))
    with pytest.raises(SkillDefaultError):
        load_all()
