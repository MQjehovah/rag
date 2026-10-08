"""编译规则解析: 模板优先(模板即编译方式), 无模板回退管道内联字段。"""
from app.core.pipeline import resolve_rule
from app.models.database import CompileTemplate, Pipeline, get_session


def _seed(api_engine):
    db = get_session(api_engine)
    try:
        db.add(CompileTemplate(id="t1", name="产品", compiler_kind="api_doc",
                               prompt="P1", rules="R1", template="T1"))
        db.add(CompileTemplate(id="t2", name="空规则", compiler_kind="wiki",
                               prompt="", rules="", template=""))
        db.add(Pipeline(id="p1", name="带模板", compiler_kind="wiki", template_id="t1",
                        scope_type="notebooks", notebook_ids="[]"))
        db.add(Pipeline(id="p2", name="无模板", compiler_kind="markdown",
                        prompt_template="PP", compile_rules="RR", compile_template="TT",
                        scope_type="notebooks", notebook_ids="[]"))
        db.add(Pipeline(id="p3", name="模板空字段", compiler_kind="wiki", template_id="t2",
                        scope_type="notebooks", notebook_ids="[]"))
        db.add(Pipeline(id="p4", name="模板不存在", compiler_kind="changelog", template_id="ghost",
                        scope_type="notebooks", notebook_ids="[]"))
        db.commit()
    finally:
        db.close()


def test_template_governs_over_pipeline_kind(api_engine):
    """模板整体接管(含 kind): 管道 kind 不再盖住模板。"""
    _seed(api_engine)
    db = get_session(api_engine)
    try:
        r = resolve_rule(db, db.get(Pipeline, "p1"))
        assert r == {"kind": "api_doc", "prompt": "P1", "rules": "R1", "template": "T1"}
    finally:
        db.close()


def test_no_template_uses_pipeline_inline(api_engine):
    """无模板: 沿用管道内联字段(历史数据兼容)。"""
    _seed(api_engine)
    db = get_session(api_engine)
    try:
        r = resolve_rule(db, db.get(Pipeline, "p2"))
        assert r == {"kind": "markdown", "prompt": "PP", "rules": "RR", "template": "TT"}
    finally:
        db.close()


def test_template_empty_fields_do_not_fall_back_to_pipeline(api_engine):
    """模板存在时其空字段不回退管道内联(交由编译步用内置默认兜底)。"""
    _seed(api_engine)
    db = get_session(api_engine)
    try:
        r = resolve_rule(db, db.get(Pipeline, "p3"))
        assert r == {"kind": "wiki", "prompt": "", "rules": "", "template": ""}
    finally:
        db.close()


def test_missing_template_falls_back_to_pipeline(api_engine):
    """template_id 指向不存在模板: 回退管道字段(不报错)。"""
    _seed(api_engine)
    db = get_session(api_engine)
    try:
        r = resolve_rule(db, db.get(Pipeline, "p4"))
        assert r["kind"] == "changelog"
    finally:
        db.close()
