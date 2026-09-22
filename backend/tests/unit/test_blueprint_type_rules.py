"""蓝图默认题型分布：优先消费考核大纲的题型比例。"""
from app.services.blueprint_persistence_service import (
    _DEFAULT_TYPE_RULES,
    _default_type_rules,
    _framework_payload,
)


class _Row:
    def __init__(self, mapping):
        self._mapping = mapping


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """按 SQL 关键词路由：考点允许题型 / 指定版本 payload / 当前已发布版本 payload。"""

    def __init__(self, allowed_types, payload, published_payload=None):
        self.allowed_types = allowed_types
        self.payload = payload
        self.published_payload = published_payload

    def execute(self, statement):
        sql = str(statement)
        lowered = sql.lower()
        if "allowed_question_types" in sql:
            return _FakeResult([_Row({"allowed_question_types": self.allowed_types})])
        # 当前已发布版本的查询带 ORDER BY version_no；指定版本的查询不带
        if "payload" in sql and "order by" in lowered:
            return _FakeResult([self.published_payload] if self.published_payload is not None else [])
        if "payload" in sql:
            return _FakeResult([self.payload] if self.payload is not None else [])
        raise AssertionError("unexpected query: " + sql[:160])


# 考纲声明的题型比例（选择题20/判断20/填空10/简答20/综合30）。
# 注意：payload 里持久化的字段名是领域模型的 final_exam_rules，不是 exam_rules。
_SYLLABUS_RULES = {
    "question_type_ratios": [
        {"question_type": "single_choice", "ratio": 20},
        {"question_type": "true_false", "ratio": 20},
        {"question_type": "fill_blank", "ratio": 10},
        {"question_type": "short_answer", "ratio": 20},
        {"question_type": "comprehensive", "ratio": 30},
    ]
}


def test_type_rules_prefer_syllabus_ratios():
    """考纲声明的题型比例必须进入蓝图，而不是套用内置默认分布。"""
    rules = _default_type_rules(
        _FakeSession(allowed_types=None, payload={"final_exam_rules": _SYLLABUS_RULES}),
        course_id="c1",
        framework_version_id="fw1",
    )
    total = sum(v["count"] * v["score"] for v in rules.values())
    assert total == 100
    # 考纲声明综合题占 30%，默认分布只给 20%
    assert rules["comprehensive"]["count"] * rules["comprehensive"]["score"] == 30


def test_type_rules_fall_back_to_defaults_without_syllabus():
    rules = _default_type_rules(
        _FakeSession(allowed_types=None, payload=None),
        course_id="c1",
        framework_version_id="fw1",
    )
    assert rules == _DEFAULT_TYPE_RULES


def test_allowed_question_types_accepts_chinese_names():
    """模型在 allowed_question_types 里写中文题型名，约束必须真的生效。"""
    rules = _default_type_rules(
        _FakeSession(allowed_types=["单选题"], payload={"final_exam_rules": _SYLLABUS_RULES}),
        course_id="c1",
        framework_version_id="fw1",
    )
    assert set(rules) == {"single_choice"}


def test_allowed_types_english_names_still_work():
    rules = _default_type_rules(
        _FakeSession(allowed_types=["single_choice", "true_false"], payload=None),
        course_id="c1",
        framework_version_id="fw1",
    )
    assert set(rules) == {"single_choice", "true_false"}


def test_type_rules_fall_back_to_published_framework_when_pinned_version_has_no_rules():
    """知识目录 pin 的旧框架版本没有题型比例时，回退到当前已发布版本的比例。

    这正是线上"命题框架显示 20/20/10/20/30、蓝图却是默认分布"的根因：
    教师在知识目录发布后又重建了框架，蓝图仍拿旧版本的 payload 推导。
    """
    old_payload = {"final_exam_rules": {}}          # 旧版本：规则空
    published_payload = {"final_exam_rules": _SYLLABUS_RULES}
    rules = _default_type_rules(
        _FakeSession(allowed_types=None, payload=old_payload, published_payload=published_payload),
        course_id="c1",
        framework_version_id="fw-old",
    )
    assert rules["comprehensive"]["count"] * rules["comprehensive"]["score"] == 30
    assert sum(v["count"] * v["score"] for v in rules.values()) == 100


def test_framework_payload_prefers_version_with_rules_over_published():
    """指定版本自己就有规则时，不要被已发布版本的规则覆盖。"""
    with_rules = {"final_exam_rules": _SYLLABUS_RULES}
    other = {"final_exam_rules": _DEFAULT_TYPE_RULES}
    payload = _framework_payload(
        _FakeSession(allowed_types=None, payload=with_rules, published_payload=other),
        course_id="c1",
        framework_version_id="fw-old",
    )
    assert payload is with_rules
