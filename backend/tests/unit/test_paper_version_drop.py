"""create_paper_version_from_generation：缺题干/缺答案的题不得写入试卷。"""
import json

from sqlalchemy.dialects import postgresql as postgresql_dialect

from app.services.paper_version_service import (
    create_paper_version_from_generation,
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

    def one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """按 SQL 关键词路由 create_paper_version_from_generation 的全部数据库交互。"""

    def __init__(self, gq_rows, plan_rows=None):
        self.gq_rows = gq_rows
        self.plan_rows = plan_rows or [
            _Row({"id": "pi1", "item_index": 1}),
            _Row({"id": "pi2", "item_index": 2}),
            _Row({"id": "pi3", "item_index": 3}),
        ]
        self.committed = False
        self.inserted_paper_items = []
        self.paper_version_metadata = None

    def execute(self, statement, *args):
        sql = str(statement)
        upper = sql.upper()
        if upper.startswith("INSERT INTO PAPER_VERSIONS"):
            # Insert.values(metadata=...) 的值在 compile 后的 params 里（需指定 dialect）。
            # 必须深拷贝模拟真实 JSON 序列化：JSON 列在 execute 那一刻就把值定格，
            # 之后再 append 不会回写。按引用捕获会让"先插行后收集"的实现也测试通过，
            # 恰好把 metadata 永远为空的 bug 测没了。
            compiled = statement.compile(dialect=postgresql_dialect.dialect())
            self.paper_version_metadata = json.loads(
                json.dumps(compiled.params.get("metadata"), ensure_ascii=False, default=str)
            )
            return _FakeResult([])
        if upper.startswith("INSERT INTO PAPER_ITEMS"):
            self.inserted_paper_items = list(args[0]) if args else []
            return _FakeResult([])
        if "MAX(" in upper:
            return _FakeResult([None])
        if "GENERATED_QUESTIONS" in upper:
            return _FakeResult(self.gq_rows)
        if "PLAN_ITEMS" in upper and " IN " in upper:
            return _FakeResult(self.plan_rows)
        if upper.startswith("SELECT") and "EXAM_PROJECTS" in upper:
            return _FakeResult([_Row({"id": "p1", "status": "generating"})])
        if upper.startswith("UPDATE"):
            return _FakeResult([])
        raise AssertionError("unexpected query: " + sql[:160])

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


def _gq(gq_id, plan_item_id, payload):
    return _Row({"id": gq_id, "plan_item_id": plan_item_id, "payload": payload})


def test_questions_without_stem_or_answer_are_dropped_and_recorded():
    gq_rows = [
        _gq("g1", "pi1", {"stem": "正常题", "answer": "A", "question_type": "single_choice"}),
        # 三道防线失守留下的占位题：只有元数据
        _gq("g2", "pi2", {"question_type": "true_false", "score": 2, "exam_point_id": "ep1"}),
        # 模型漏答案的题
        _gq("g3", "pi3", {"stem": "有题干没答案", "answer": "", "question_type": "fill_blank"}),
    ]
    session = _FakeSession(gq_rows)

    create_paper_version_from_generation(
        session,
        course_id="c1",
        project_id="p1",
        generation_run_id="run1",
        questions_list=[
            {"plan_item_id": "pi1", "item_index": 1, "quality": {"status": "pass"}},
            {"plan_item_id": "pi2", "item_index": 2, "quality": {"status": "blocker", "message": "生成失败"}},
            {"plan_item_id": "pi3", "item_index": 3, "quality": {"status": "blocker", "message": "缺答案"}},
        ],
    )

    # 只有一道题落库，且题号连续（没有空洞）
    assert [r["display_order"] for r in session.inserted_paper_items] == [1]
    assert session.committed is True

    dropped = session.paper_version_metadata["dropped_slots"]
    assert len(dropped) == 2
    reasons = {d["reason"] for d in dropped}
    assert reasons == {"missing_stem", "missing_answer"}
    by_reason = {d["reason"]: d for d in dropped}
    assert by_reason["missing_stem"]["item_index"] == 2
    assert by_reason["missing_answer"]["item_index"] == 3


def test_all_usable_questions_have_empty_dropped_slots():
    gq_rows = [
        _gq("g1", "pi1", {"stem": "题干一", "answer": True, "question_type": "true_false"}),
        _gq("g2", "pi2", {"stem": "题干二", "answer": "有监督微调", "question_type": "fill_blank"}),
    ]
    session = _FakeSession(gq_rows)

    create_paper_version_from_generation(
        session,
        course_id="c1",
        project_id="p1",
        generation_run_id="run1",
        questions_list=[
            {"plan_item_id": "pi1", "item_index": 1, "quality": {"status": "pass"}},
            {"plan_item_id": "pi2", "item_index": 2, "quality": {"status": "pass"}},
        ],
    )
    assert [r["display_order"] for r in session.inserted_paper_items] == [1, 2]
    assert session.paper_version_metadata["dropped_slots"] == []


def test_question_without_generated_question_row_is_recorded_as_gap():
    # gq 行缺失曾经静默 continue：题位既不落卷也不留痕，是最难查的一种少题。
    session = _FakeSession([])

    create_paper_version_from_generation(
        session,
        course_id="c1",
        project_id="p1",
        generation_run_id="run1",
        questions_list=[
            {"plan_item_id": "pi1", "item_index": 1, "quality": {"status": "pass"}},
        ],
    )

    assert session.inserted_paper_items == []
    dropped = session.paper_version_metadata["dropped_slots"]
    assert [d["reason"] for d in dropped] == ["missing_generated_question"]
    assert dropped[0]["item_index"] == 1
