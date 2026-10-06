"""课程删除：删除顺序必须满足外键依赖（子表先于父表）+ 先解除 FK 环。

线上首页删课程 500 的根因：delete_course 按 metadata.sorted_tables 正向
（建表序 = 父表在前）删除，exam_projects 先于 blueprint_versions 被删，
触发 ForeignKeyViolation（blueprint_versions_exam_project_id_fkey）。
另外 exam_projects 的 active_* 反向引用与 blueprint_versions / generation_runs /
paper_versions 构成不可延迟的复合外键环，删这些父表之前必须先置空
（与 exam_project_service.delete_project 同一处置）。

这里用「外键图 + 录制操作顺序」静态验证，不依赖真实数据库。
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import Table

import app.db.schema as schema_mod
from app.db.schema import Base
from app.services import course_service as svc

# 环边：exam_projects 的三个反向引用（use_alter 复合外键），置空即解除
_CYCLE_COLUMNS = {
    "active_blueprint_version_id",
    "active_generation_run_id",
    "active_paper_version_id",
}


class _Result:
    def all(self):
        return []


class _FakeCourse:
    id = "c1"
    owner_id = "o1"


class _RecordingSession:
    """假 session：按 SQL 记录「置空 / 删除」的操作序列，供顺序静态验证。"""

    def __init__(self):
        self.ops: list[tuple[str, str]] = []  # ("null", "表.列") / ("delete", "表")
        self.deleted_course = False

    def scalar(self, statement):
        return _FakeCourse()

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        upper = sql.upper()
        if upper.startswith("DELETE FROM"):
            self.ops.append(("delete", sql.split()[2]))
        elif upper.startswith("UPDATE"):
            table = sql.split()[1]
            set_part = sql.split(" SET ", 1)[1].split(" WHERE ")[0]
            for assignment in set_part.split(","):
                col = assignment.strip().split("=")[0].strip().split(".")[-1]
                self.ops.append(("null", f"{table}.{col}"))
        return _Result()

    def delete(self, obj):
        self.deleted_course = True

    def commit(self):
        pass


def _fk_graph():
    """返回 (子表 -> 父表 -> 子表上的引用列集合)，与 test_exam_project_delete 同款。"""
    tables: dict[str, Table] = {}
    for name in dir(schema_mod):
        obj = getattr(schema_mod, name)
        if isinstance(obj, Table):
            tables[obj.name] = obj
    edges: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for table in tables.values():
        for fk in table.foreign_keys:
            edges[table.name][fk.column.table.name].add(fk.parent.name)
    return edges


def _run() -> tuple[_RecordingSession, dict[str, int], dict[str, int]]:
    session = _RecordingSession()
    svc.delete_course(session, owner_id="o1", course_id="c1")
    order: dict[str, int] = {}
    nulled_at: dict[str, int] = {}
    for i, (kind, name) in enumerate(session.ops):
        if kind == "delete":
            order.setdefault(name, i)
        else:
            nulled_at.setdefault(name, i)
    return session, order, nulled_at


def test_delete_course_satisfies_every_foreign_key():
    _, order, nulled_at = _run()
    assert order, "删除序列不应为空"

    violations = []
    for child, parents in _fk_graph().items():
        if child not in order:
            continue
        for parent, cols in parents.items():
            if parent not in order:
                continue
            if child == parent:
                # 自引用（content_domains.parent_domain_id / document_parse_runs.reused_from_run_id）：
                # 单条 DELETE ... WHERE course_id 一次性删光该表全部分行，
                # 约束按语句结束时校验，不存在悬空引用
                continue
            if order[child] < order[parent]:
                continue  # 子表先删，满足
            # 顺序不满足时，要求 child 的引用列在删 parent 之前被置空。
            # 复合外键遵 Match SIMPLE：任一列为 NULL 即不再受约束，
            # 因此置空任一列即可（环边置空的是 active_* 三列之一）。
            satisfied = any(
                f"{child}.{c}" in nulled_at and nulled_at[f"{child}.{c}"] < order[parent]
                for c in cols
            )
            if not satisfied:
                violations.append(
                    f"{child} 引用 {parent}（列 {','.join(sorted(cols))}）："
                    "既未先删 child，也未先置空其中任一列"
                )
    assert not violations, "；".join(violations)


def test_delete_course_covers_every_course_table():
    """课程域数据必须全部清掉，不能留下悬空引用。"""
    session, _, _ = _run()
    expected = {t.name for t in Base.metadata.sorted_tables if "course_id" in t.c}
    deleted = {name for kind, name in session.ops if kind == "delete"}
    missing = expected - deleted
    assert not missing, "漏删表: " + ",".join(sorted(missing))
    assert session.deleted_course, "课程行本身必须删除"


def test_delete_course_nulls_active_back_references_before_any_delete():
    """FK 环的解除方式：三个 active_* 列必须在删任何表之前置空。"""
    session, order, nulled_at = _run()
    first_delete = min(order.values())
    for col in _CYCLE_COLUMNS:
        key = f"exam_projects.{col}"
        assert key in nulled_at, f"{key} 未被置空"
        assert nulled_at[key] < first_delete, f"{key} 必须在任何删除之前置空"