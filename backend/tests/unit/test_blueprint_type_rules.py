"""蓝图默认题型分布：优先消费考核大纲的题型比例。"""
from app.services.blueprint_persistence_service import (
    _DEFAULT_TYPE_RULES,
    _default_type_rules,
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
    """按 SQL 关键词路由：考点允许题型查询 / 框架 payload 查询。"""

    def __init__(self, allowed_types, payload):
        self.allowed_types = allowed_types
        self.payload = payload

    def execute(self, statement):
        sql = str(statement)
        if "allowed_question_types" in sql:
            return _FakeResult([
                _Row({"allowed_question_types": self.allowed_types}) if self.allowed_types is not None else _Row({"allowed_question_types": None})
            ])
        if "payload" in sql:
            return _FakeResult([self.payload] if self.payload is not None else [])
        raise AssertionError("unexpected query: " + sql[:120])


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
