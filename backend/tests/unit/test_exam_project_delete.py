"""项目删除：删除顺序必须满足外键依赖（子表先于父表，或先置空引用列）。

线上曾因两类问题 DELETE 500：
1. 漏删 model_calls（引用 generation_attempts）与 outbox_events（引用 task_runs）；
2. exam_projects 的 active_* 反向引用 blueprint_versions/generation_runs/paper_versions，
   与它们构成不可延迟的 FK 环——不先置空就删那三张表会直接触发外键约束失败。

这里用「外键图 + 录制删除/置空顺序」静态验证，不依赖真实数据库。
"""
from collections import defaultdict

from sqlalchemy import Table

import app.db.schema as schema_mod
from app.services import exam_project_service as svc


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _RecordingSession:
    """假 session：按 SQL 路由返回假 id，使删除分支全部走到；记录 DELETE 与被置空的列。"""

    PROJECT_ROW = {
        "id": "p1", "course_id": "c1", "name": "demo", "status": "review",
        "active_blueprint_version_id": "bv1", "active_paper_version_id": "pv1",
        "active_generation_run_id": "run1",
    }

    def __init__(self):
        self.deleted: list[str] = []
        self.nulled: list[tuple[str, str]] = []  # (表, 被置空的列)

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        upper = sql.upper()
        if upper.startswith("DELETE FROM"):
            self.deleted.append(sql.split()[2])
            return _Result([])
        if upper.startswith("UPDATE"):
            table = sql.split()[1]
            set_part = sql.split(" SET ", 1)[1].split(" WHERE ")[0]
            for assignment in set_part.split(","):
                col = assignment.strip().split("=")[0].strip().split(".")[-1]
                self.nulled.append((table, col))
            return _Result([])
        if "GENERATION_ATTEMPTS" in upper:
            return _Result(["att1"])
        if "GENERATED_QUESTIONS" in upper:
            return _Result(["gq1"])
        if "GENERATION_RUNS" in upper:
            return _Result(["run1"])
        if "PAPER_VERSIONS" in upper:
            return _Result(["pv1"] if "PAPER_VERSIONS.EXAM_PROJECT_ID," not in upper and "EXAM_PROJECT_ID" in upper else [])
        if "TASK_RUNS" in upper:
            return _Result(["tr1"] if "PROJECT_ID" in upper else [])
        if "BLUEPRINT_VERSIONS" in upper:
            return _Result(["bv1"] if "EXAM_PROJECT_ID" in upper else [])
        if "EXAM_PROJECTS" in upper:
            return _Result([dict(self.PROJECT_ROW)])
        return _Result([])

    def commit(self):
        pass

    def rollback(self):
        pass


def _fk_graph():
    """返回 (子表 -> 父表 -> 子表上的引用列名)。"""
    tables: dict[str, Table] = {}
    for name in dir(schema_mod):
        obj = getattr(schema_mod, name)
        if isinstance(obj, Table):
            tables[obj.name] = obj
    edges: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for table in tables.values():
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            col = fk.parent.name
            edges[table.name][parent].add(col)
    return edges


def test_delete_satisfies_every_foreign_key():
    session = _RecordingSession()
    svc.delete_project(session, course_id="c1", project_id="p1")

    assert session.deleted, "删除序列不应为空"
    order = {t: i for i, t in enumerate(session.deleted)}
    nulled_at = {}
    for i, (table, col) in enumerate(session.nulled):
        nulled_at.setdefault((table, col), i)
    edges = _fk_graph()

    violations = []
    for child, parents in edges.items():
        if child not in order:
            continue  # 该项目不涉及该子表
        for parent, cols in parents.items():
            if parent not in order:
                continue
            if order[child] < order[parent]:
                continue  # 子表先删，满足
            # 顺序不满足时，要求 child 的引用列在删 parent 之前被置空。
            # 复合外键（如 [active_paper_version_id, course_id]）遵 Match SIMPLE：
            # 任一列为 NULL 即不再受约束，因此置空任一列即可。
            satisfied = any(
                (child, c) in nulled_at and nulled_at[(child, c)] < order[parent]
                for c in cols
            )
            if not satisfied:
                violations.append(
                    f"{child} 引用 {parent}（列 {','.join(sorted(cols))}）："
                    "既未先删 child，也未先置空其中任一列"
                )
    assert not violations, "；".join(violations)


def test_delete_covers_every_derived_table():
    """项目派生数据必须全部清掉，不能留下悬空引用。"""
    session = _RecordingSession()
    svc.delete_project(session, course_id="c1", project_id="p1")
    expected = {
        "model_calls", "quality_checks", "paper_items", "generated_questions",
        "paper_versions", "generation_attempts", "generation_runs", "outbox_events",
        "plan_items", "blueprint_sections", "blueprint_versions", "task_runs",
        "exam_projects",
    }
    missing = expected - set(session.deleted)
    assert not missing, "漏删表: " + ",".join(sorted(missing))


def test_delete_nulls_active_back_references_first():
    """FK 环的解除方式：先置空 exam_projects 的三个 active_* 列。"""
    session = _RecordingSession()
    svc.delete_project(session, course_id="c1", project_id="p1")
    nulled_cols = {col for table, col in session.nulled if table == "exam_projects"}
    assert {"active_blueprint_version_id", "active_generation_run_id", "active_paper_version_id"} <= nulled_cols
