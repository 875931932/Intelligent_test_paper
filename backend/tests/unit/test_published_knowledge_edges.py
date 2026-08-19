"""relation_edges 持久化测试。

验证 knowledge_cards 表含 relation_edges JSON 列，且发布时 _insert_tree
会把 card.relation_edges 写入对应的 knowledge_cards 行。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.db.schema import knowledge_cards
from app.domain.generation.semantic_diversity import AnswerRelation
from app.domain.knowledge.models import (
    AssessmentUnitDraft,
    KnowledgeCardDraft,
    KnowledgeTopicDraft,
    KnowledgeTreeCandidate,
)
from app.services.knowledge_publish_service import DatabaseKnowledgeRepository


def test_knowledge_cards_table_has_relation_edges_column():
    """schema 中 knowledge_cards 表必须含 relation_edges JSON 列。"""
    cols = {c.name for c in knowledge_cards.columns}
    assert "relation_edges" in cols, "knowledge_cards 缺少 relation_edges 列"


def _make_tree_with_relation_edges() -> KnowledgeTreeCandidate:
    card = KnowledgeCardDraft(
        name="Card A",
        performance_statement="ps",
        assessable_content=["fact1"],
        concept_cluster="cluster-a",
        answer_proposition="prop",
        relation_edges=[
            AnswerRelation(kind="equivalent_to", target="Card B"),
        ],
    )
    unit = AssessmentUnitDraft(
        code="U1",
        title="Unit 1",
        performance_statement="ps",
        exam_point_code="EP1",
        cards=[card],
    )
    topic = KnowledgeTopicDraft(
        code="T1",
        name="Topic 1",
        framework_anchor_key="k1",
        units=[unit],
    )
    return KnowledgeTreeCandidate(
        framework_version_id="fw1",
        topics=[topic],
    )


def _is_knowledge_cards_insert(stmt) -> bool:
    table = getattr(stmt, "table", None)
    if table is not None:
        return getattr(table, "name", None) == "knowledge_cards"
    return "knowledge_cards" in str(stmt)


def test_insert_tree_writes_relation_edges():
    """_insert_tree 必须把 card.relation_edges 写入 knowledge_cards 行。"""
    session = MagicMock()
    repo = DatabaseKnowledgeRepository(session)
    repo._insert_tree(
        course_id="c1",
        catalog_id="cat1",
        tree=_make_tree_with_relation_edges(),
        allowed={"k1"},
        point_ids={"EP1": "ep-id-1"},
        publishable_exam_point_codes=None,
    )

    card_inserts = [
        call.args[0]
        for call in session.execute.call_args_list
        if _is_knowledge_cards_insert(call.args[0])
    ]
    assert card_inserts, "未执行 knowledge_cards.insert()"

    params = card_inserts[0].compile().params
    assert "relation_edges" in params, "knowledge_cards.insert() 未写入 relation_edges"
    assert params["relation_edges"] == [
        {"kind": "equivalent_to", "target": "Card B"},
    ]
