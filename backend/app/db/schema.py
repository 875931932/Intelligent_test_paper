"""SQLAlchemy metadata for the fresh, course-isolated core database.

This is an initial schema, not a legacy migration layer.  It intentionally
defines only the durable data skeleton needed by subsequent tasks.
"""

from __future__ import annotations

from hashlib import sha1

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Table, Text, UniqueConstraint, false, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _id_column() -> Column[str]:
    return Column("id", String(64), primary_key=True)


def _course_id_column() -> Column[str]:
    return Column("course_id", String(64), ForeignKey("courses.id"), nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 鉴权字段：真实账号必须填写；历史/测试种子行可缺省（登录路径会强制校验）
    username: Mapped[str | None] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="teacher")


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = (
        UniqueConstraint("owner_id", "slug", name="uq_courses_owner_slug"),
        Index("ix_courses_owner_id", "owner_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


def _course_table(name: str, *columns: Column, constraints: tuple = ()) -> Table:
    """Create a course-owned table and its mandatory tenant lookup index."""

    table = Table(
        name,
        Base.metadata,
        _id_column(),
        _course_id_column(),
        *columns,
        UniqueConstraint("id", "course_id", name=f"uq_{name}_id_course"),
        *constraints,
    )
    Index(f"ix_{name}_course_id", table.c.course_id)
    return table


# Files and uploads
materials = _course_table(
    "materials",
    Column("logical_name", String(255), nullable=False),
    Column("material_type", String(40), nullable=False),
    Column("status", String(40), nullable=False, default="staged", server_default="staged"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    constraints=(
        UniqueConstraint("course_id", "logical_name", name="uq_materials_course_name"),
        CheckConstraint(
            "material_type IN ('teaching_syllabus', 'assessment_syllabus', 'teaching_material', 'exercise')",
            name="ck_materials_material_type",
        ),
        CheckConstraint("status IN ('staged', 'deleted')", name="ck_materials_status"),
    ),
)
Index("ix_materials_course_status", materials.c.course_id, materials.c.status)

material_versions = _course_table(
    "material_versions",
    Column("material_id", String(64), ForeignKey("materials.id"), nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("mime_type", String(200), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("status", String(40), nullable=False, default="staged", server_default="staged"),
    Column("object_key", String(500), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    constraints=(
        UniqueConstraint("material_id", "version_no", name="uq_material_versions_number"),
        CheckConstraint("version_no > 0", name="ck_material_versions_version_no"),
        CheckConstraint("size_bytes > 0", name="ck_material_versions_size_bytes"),
        CheckConstraint("length(sha256) = 64", name="ck_material_versions_sha256_length"),
        CheckConstraint("status IN ('staged', 'deleted')", name="ck_material_versions_status"),
    ),
)
Index("ix_material_versions_course_material_status", material_versions.c.course_id, material_versions.c.material_id, material_versions.c.status)

upload_sessions = _course_table(
    "upload_sessions",
    Column("material_id", String(64), ForeignKey("materials.id")),
    Column("material_version_id", String(64), ForeignKey("material_versions.id")),
    Column("session_key", String(128), nullable=False),
    Column("filename", String(255), nullable=False),
    Column("material_type", String(40), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("mime_type", String(200), nullable=False),
    Column("object_key", String(500), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    Column("status", String(40), nullable=False, default="pending", server_default="pending"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    constraints=(
        UniqueConstraint("session_key", name="uq_upload_sessions_session_key"),
        CheckConstraint(
            "material_type IN ('teaching_syllabus', 'assessment_syllabus', 'teaching_material', 'exercise')",
            name="ck_upload_sessions_material_type",
        ),
        CheckConstraint("size_bytes > 0", name="ck_upload_sessions_size_bytes"),
        CheckConstraint("length(sha256) = 64", name="ck_upload_sessions_sha256_length"),
        CheckConstraint("status IN ('pending', 'completing', 'completed')", name="ck_upload_sessions_status"),
    ),
)
Index("ix_upload_sessions_course_status_expires", upload_sessions.c.course_id, upload_sessions.c.status, upload_sessions.c.expires_at)

# Document parsing
parser_profiles = _course_table(
    "parser_profiles",
    Column("name", String(120), nullable=False),
    Column("version", String(80), nullable=False),
    Column("provider", String(80), nullable=False),
    Column("configuration", JSON, nullable=False, default=dict),
    constraints=(UniqueConstraint("course_id", "name", "version", name="uq_parser_profiles_course_name_version"),),
)

document_parse_runs = _course_table(
    "document_parse_runs",
    Column("material_version_id", String(64), ForeignKey("material_versions.id"), nullable=False),
    Column("parser_profile_id", String(64), ForeignKey("parser_profiles.id"), nullable=False),
    Column("reused_from_run_id", String(64), ForeignKey("document_parse_runs.id")),
    Column("status", String(40), nullable=False, default="queued", server_default="queued"),
    Column("provider_run_id", String(160)),
    Column("trace_id", String(160)),
    Column("error_code", String(120)),
    Column("error_summary", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    constraints=(
        CheckConstraint("status IN ('queued', 'submitted', 'waiting_file', 'pending', 'running', 'converting', 'ready', 'failed')", name="ck_document_parse_runs_status"),
    ),
)
Index("ix_document_parse_runs_course_status", document_parse_runs.c.course_id, document_parse_runs.c.status)

document_artifacts = _course_table(
    "document_artifacts",
    Column("document_parse_run_id", String(64), ForeignKey("document_parse_runs.id"), nullable=False),
    Column("artifact_type", String(60), nullable=False),
    Column("storage_key", String(500), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    constraints=(UniqueConstraint("document_parse_run_id", "artifact_type", name="uq_document_artifacts_run_type"),),
)

content_blocks = _course_table(
    "content_blocks",
    Column("document_parse_run_id", String(64), ForeignKey("document_parse_runs.id"), nullable=False),
    Column("material_version_id", String(64), ForeignKey("material_versions.id"), nullable=False),
    Column("block_index", Integer, nullable=False),
    Column("block_type", String(60), nullable=False),
    Column("text", Text, nullable=False, default=""),
    Column("markdown", Text),
    Column("latex", Text),
    Column("page_index", Integer),
    Column("bbox", JSON),
    Column("heading_path", JSON, nullable=False, default=list),
    Column("asset_reference", String(500)),
    Column("reading_order", Integer, nullable=False),
    Column("content_hash", String(64), nullable=False),
    constraints=(UniqueConstraint("document_parse_run_id", "block_index", name="uq_content_blocks_run_index"),),
)
Index("ix_content_blocks_course_material_block", content_blocks.c.course_id, content_blocks.c.material_version_id, content_blocks.c.block_index)

# Framework construction
framework_build_runs = _course_table(
    "framework_build_runs",
    Column("status", String(40), nullable=False, default="queued", server_default="queued"),
    Column("input_snapshot", JSON, nullable=False, default=dict),
    Column("error_code", String(80)),
    Column("error_message", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    constraints=(
        CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_teacher_confirmation', 'published', 'rejected', 'failed', 'cancelled')",
            name="ck_framework_build_runs_status",
        ),
    ),
)

framework_versions = _course_table(
    "framework_versions",
    Column("framework_build_run_id", String(64), ForeignKey("framework_build_runs.id")),
    Column("version_no", Integer, nullable=False),
    Column("status", String(40), nullable=False, default="candidate", server_default="candidate"),
    Column("payload", JSON, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("published_at", DateTime(timezone=True)),
    constraints=(
        UniqueConstraint("course_id", "version_no", name="uq_framework_versions_number"),
        UniqueConstraint("course_id", "framework_build_run_id", name="uq_framework_versions_build_run"),
        CheckConstraint("version_no >= 1", name="ck_framework_versions_version_no"),
        CheckConstraint(
            "status IN ('candidate', 'published', 'superseded', 'rejected')",
            name="ck_framework_versions_status",
        ),
    ),
)
Index(
    "uq_framework_versions_current_published",
    framework_versions.c.course_id,
    unique=True,
    postgresql_where=framework_versions.c.status == "published",
    sqlite_where=framework_versions.c.status == "published",
)

framework_anchors = _course_table(
    "framework_anchors",
    Column("framework_version_id", String(64), ForeignKey("framework_versions.id"), nullable=False),
    Column("anchor_type", String(60), nullable=False),
    Column("anchor_key", String(160), nullable=False),
    Column("payload", JSON, nullable=False, default=dict),
    constraints=(UniqueConstraint("framework_version_id", "anchor_type", "anchor_key", name="uq_framework_anchors_version_key"),),
)

framework_conflicts = _course_table(
    "framework_conflicts",
    Column("framework_version_id", String(64), ForeignKey("framework_versions.id"), nullable=False),
    Column("status", String(40), nullable=False, default="open"),
    Column("details", JSON, nullable=False, default=dict),
)

exam_points = _course_table(
    "exam_points",
    Column("framework_version_id", String(64), ForeignKey("framework_versions.id"), nullable=False),
    Column("anchor_key", String(160), nullable=False),
    Column("code", String(100), nullable=False),
    Column("title", String(255), nullable=False),
    Column("assessment_requirement", Text, nullable=False),
    Column("weight_value", Float, nullable=False, default=0, server_default="0"),
    Column("weight_source", String(40), nullable=False),
    Column("weight_group_id", String(160), nullable=False),
    Column("priority", String(40), nullable=False, default="normal", server_default="normal"),
    Column("cognitive_targets", JSON, nullable=False, default=list),
    Column("assessment_orientations", JSON, nullable=False, default=list),
    Column("allowed_question_types", JSON, nullable=False, default=list),
    Column(
        "operational_detail_policy",
        String(40),
        nullable=False,
        default="supporting_only",
        server_default="supporting_only",
    ),
    Column("scope_boundary", JSON, nullable=False, default=dict),
    Column("required_evidence_roles", JSON, nullable=False, default=list),
    Column("retrieval_intent", Text, nullable=False),
    Column("teaching_anchor_keys", JSON, nullable=False, default=list),
    Column("status", String(40), nullable=False, default="candidate", server_default="candidate"),
    constraints=(
        UniqueConstraint("framework_version_id", "code", name="uq_exam_points_version_code"),
        CheckConstraint("weight_value >= 0 AND weight_value <= 100", name="ck_exam_points_weight"),
        CheckConstraint(
            "weight_source IN ('assessment_syllabus', 'inherited_group', 'teacher_confirmed')",
            name="ck_exam_points_weight_source",
        ),
        CheckConstraint(
            "operational_detail_policy IN ('forbidden', 'supporting_only', 'directly_assessable')",
            name="ck_exam_points_operational_detail_policy",
        ),
    ),
)
Index("ix_exam_points_course_framework_status", exam_points.c.course_id, exam_points.c.framework_version_id, exam_points.c.status)

# Organization and evidence
organization_runs = _course_table(
    "organization_runs",
    Column("framework_version_id", String(64), ForeignKey("framework_versions.id")),
    Column("status", String(40), nullable=False, default="queued", server_default="queued"),
    Column("input_snapshot", JSON, nullable=False, default=dict),
    Column("error_code", String(80)),
    Column("error_message", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    constraints=(
        CheckConstraint("status IN ('queued', 'running', 'awaiting_teacher_confirmation', 'published', 'rejected', 'failed')", name="ck_organization_runs_status"),
    ),
)

evidence_chunks = _course_table(
    "evidence_chunks",
    Column("organization_run_id", String(64), ForeignKey("organization_runs.id"), nullable=False),
    Column("material_version_id", String(64), ForeignKey("material_versions.id"), nullable=False),
    Column("content_block_id", String(64), ForeignKey("content_blocks.id")),
    Column("chunk_index", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("locator", JSON),
    Column("embedding", JSON),
    constraints=(
        UniqueConstraint("organization_run_id", "chunk_index", name="uq_evidence_chunks_run_index"),
        UniqueConstraint(
            "id",
            "organization_run_id",
            "course_id",
            name="uq_evidence_chunks_id_run_course",
        ),
    ),
)
Index("ix_evidence_chunks_course_material_hash", evidence_chunks.c.course_id, evidence_chunks.c.material_version_id, evidence_chunks.c.content_hash)

exam_point_evidence_links = _course_table(
    "exam_point_evidence_links",
    Column("organization_run_id", String(64), ForeignKey("organization_runs.id"), nullable=False),
    Column("exam_point_id", String(64), ForeignKey("exam_points.id"), nullable=False),
    Column("evidence_chunk_id", String(64), ForeignKey("evidence_chunks.id"), nullable=False),
    Column("relevance_class", String(40), nullable=False),
    Column("support_claim", Text, nullable=False),
    Column("evidence_role", String(60)),
    Column("confidence", Integer),
    Column("prompt_material", Text),
    Column("status", String(40), nullable=False, default="candidate", server_default="candidate"),
    constraints=(
        UniqueConstraint(
            "organization_run_id",
            "exam_point_id",
            "evidence_chunk_id",
            name="uq_exam_point_evidence_links_run_point_chunk",
        ),
        ForeignKeyConstraint(
            ["evidence_chunk_id", "organization_run_id", "course_id"],
            ["evidence_chunks.id", "evidence_chunks.organization_run_id", "evidence_chunks.course_id"],
            name="fk_exam_point_evidence_links_chunk_run_course",
        ),
        CheckConstraint(
            "relevance_class IN ('direct', 'supporting', 'background', 'out_of_scope')",
            name="ck_exam_point_evidence_links_relevance_class",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 100)",
            name="ck_exam_point_evidence_links_confidence",
        ),
    ),
)
Index(
    "ix_exam_point_evidence_links_course_point_relevance",
    exam_point_evidence_links.c.course_id,
    exam_point_evidence_links.c.exam_point_id,
    exam_point_evidence_links.c.relevance_class,
)

# Knowledge catalogue and the source-free KnowledgeCard boundary
knowledge_catalog_versions = _course_table(
    "knowledge_catalog_versions",
    Column("organization_run_id", String(64), ForeignKey("organization_runs.id")),
    Column("framework_version_id", String(64), ForeignKey("framework_versions.id"), nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("status", String(40), nullable=False, default="candidate", server_default="candidate"),
    Column("payload", JSON, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("published_at", DateTime(timezone=True)),
    constraints=(
        UniqueConstraint("course_id", "version_no", name="uq_knowledge_catalog_versions_number"),
        UniqueConstraint("course_id", "organization_run_id", name="uq_knowledge_catalog_versions_organization_run"),
        CheckConstraint("version_no >= 1", name="ck_knowledge_catalog_versions_version_no"),
        CheckConstraint("status IN ('candidate', 'published', 'superseded', 'rejected')", name="ck_knowledge_catalog_versions_status"),
    ),
)

content_domains = _course_table(
    "content_domains",
    Column("catalog_version_id", String(64), ForeignKey("knowledge_catalog_versions.id"), nullable=False),
    Column("parent_domain_id", String(64), ForeignKey("content_domains.id")),
    Column("level", Integer, nullable=False),
    Column("framework_anchor_key", String(160), nullable=False),
    Column("code", String(100), nullable=False),
    Column("name", String(200), nullable=False),
    Column("status", String(40), nullable=False, default="active", server_default="active"),
    constraints=(
        UniqueConstraint("catalog_version_id", "code", name="uq_content_domains_catalog_code"),
        CheckConstraint("level IN (1, 2)", name="ck_content_domains_level"),
        CheckConstraint("status IN ('active', 'excluded', 'needs_teacher_review')", name="ck_content_domains_status"),
    ),
)

assessment_units = _course_table(
    "assessment_units",
    Column("catalog_version_id", String(64), ForeignKey("knowledge_catalog_versions.id"), nullable=False),
    Column("content_domain_id", String(64), ForeignKey("content_domains.id")),
    Column("exam_point_id", String(64), ForeignKey("exam_points.id")),
    Column("code", String(100), nullable=False),
    Column("title", String(255), nullable=False),
    Column("performance_statement", Text, nullable=False),
    Column("scope_boundary", JSON, nullable=False, default=dict),
    Column("weight", Integer),
    Column("status", String(40), nullable=False, default="active", server_default="active"),
    constraints=(UniqueConstraint("catalog_version_id", "code", name="uq_assessment_units_catalog_code"),),
)

knowledge_cards = _course_table(
    "knowledge_cards",
    Column("catalog_version_id", String(64), ForeignKey("knowledge_catalog_versions.id"), nullable=False),
    Column("assessment_unit_id", String(64), ForeignKey("assessment_units.id"), nullable=False),
    Column("name", String(255), nullable=False),
    Column("performance_statement", Text, nullable=False),
    Column("assessable_content", JSON, nullable=False, default=list),
    Column("scope_boundary", JSON, nullable=False, default=dict),
    Column("cognitive_targets", JSON, nullable=False, default=list),
    Column("allowed_question_types", JSON, nullable=False, default=list),
    Column("importance", Integer, nullable=False, default=1),
    # 语义画像字段：concept_cluster 供合同聚类强制同簇，answer_proposition 供
    # 答案域互斥检测，prompt_material 供命题上下文回退——发布时必须一并持久化，
    # 否则数据库链路丢失信号后防重复机制静默退化。
    Column("concept_cluster", String(255), nullable=False, default="", server_default=""),
    Column("answer_proposition", Text, nullable=False, default="", server_default=""),
    Column("prompt_material", JSON, nullable=False, default=list, server_default="[]"),
    # 语义关系边：图谱视图绘制卡片间 specializes/requires/contrasts_with 等关系。
    Column("relation_edges", JSON, nullable=False, default=list, server_default="[]"),
    Column("content_hash", String(64), nullable=False),
    Column("status", String(40), nullable=False, default="draft"),
    Column("version", Integer, nullable=False, default=1),
    constraints=(UniqueConstraint("catalog_version_id", "content_hash", "version", name="uq_knowledge_cards_catalog_hash_version"),),
)
Index("ix_knowledge_cards_course_catalog_status", knowledge_cards.c.course_id, knowledge_cards.c.catalog_version_id, knowledge_cards.c.status)

knowledge_evidence_links = _course_table(
    "knowledge_evidence_links",
    Column("knowledge_card_id", String(64), ForeignKey("knowledge_cards.id"), nullable=False),
    Column("evidence_chunk_id", String(64), ForeignKey("evidence_chunks.id"), nullable=False),
    Column("evidence_role", String(60), nullable=False),
    Column("confidence", Integer),
    Column("teacher_confirmed", Boolean, nullable=False, server_default=false()),
    Column("lifecycle_status", String(40), nullable=False, default="active"),
    constraints=(UniqueConstraint("knowledge_card_id", "evidence_chunk_id", "evidence_role", name="uq_knowledge_evidence_link"),),
)

# Published retrieval index
index_versions = _course_table(
    "index_versions",
    Column("catalog_version_id", String(64), ForeignKey("knowledge_catalog_versions.id"), nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("status", String(40), nullable=False, default="draft"),
    constraints=(UniqueConstraint("course_id", "version_no", name="uq_index_versions_number"),),
)

index_memberships = _course_table(
    "index_memberships",
    Column("index_version_id", String(64), ForeignKey("index_versions.id"), nullable=False),
    Column("knowledge_card_id", String(64), ForeignKey("knowledge_cards.id"), nullable=False),
    constraints=(UniqueConstraint("index_version_id", "knowledge_card_id", name="uq_index_memberships_version_card"),),
)

# Blueprint and project planning
exam_projects = _course_table(
    "exam_projects",
    Column("name", String(255), nullable=False),
    Column("status", String(40), nullable=False, default="draft"),
    Column("active_blueprint_version_id", String(64)),
    Column("active_generation_run_id", String(64)),
    Column("active_paper_version_id", String(64)),
    constraints=(UniqueConstraint("course_id", "name", name="uq_exam_projects_course_name"),),
)

blueprint_versions = _course_table(
    "blueprint_versions",
    Column("exam_project_id", String(64), ForeignKey("exam_projects.id"), nullable=False),
    Column("framework_version_id", String(64), ForeignKey("framework_versions.id"), nullable=False),
    Column("catalog_version_id", String(64), ForeignKey("knowledge_catalog_versions.id"), nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("status", String(40), nullable=False, default="draft"),
    Column("type_rules", JSON, nullable=False, default=dict, server_default="{}"),
    Column("chapter_weights", JSON, nullable=False, default=dict, server_default="{}"),
    Column("confirmed_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    constraints=(UniqueConstraint("exam_project_id", "version_no", name="uq_blueprint_versions_project_number"),),
)

blueprint_sections = _course_table(
    "blueprint_sections",
    Column("blueprint_version_id", String(64), ForeignKey("blueprint_versions.id"), nullable=False),
    Column("content_domain_id", String(64), ForeignKey("content_domains.id")),
    Column("section_index", Integer, nullable=False),
    Column("score", Integer),
    constraints=(UniqueConstraint("blueprint_version_id", "section_index", name="uq_blueprint_sections_version_index"),),
)

plan_items = _course_table(
    "plan_items",
    Column("blueprint_version_id", String(64), ForeignKey("blueprint_versions.id"), nullable=False),
    Column("blueprint_section_id", String(64), ForeignKey("blueprint_sections.id")),
    Column("assessment_unit_id", String(64), ForeignKey("assessment_units.id"), nullable=False),
    Column("question_type", String(60), nullable=False),
    Column("assessment_mode", String(60), nullable=False, default="conceptual", server_default="conceptual"),
    Column("item_index", Integer, nullable=False),
    Column("score", Float, nullable=False),
    Column("difficulty", String(40), nullable=False, default="medium", server_default="medium"),
    Column("cognitive_level", String(60), nullable=False, default="understand", server_default="understand"),
    Column("exam_point_id", String(64), ForeignKey("exam_points.id")),
    Column("knowledge_card_id", String(64), ForeignKey("knowledge_cards.id")),
    constraints=(UniqueConstraint("blueprint_version_id", "item_index", name="uq_plan_items_version_index"),),
)
Index("ix_plan_items_course_blueprint_question_type", plan_items.c.course_id, plan_items.c.blueprint_version_id, plan_items.c.question_type)

# Generation, audit, review and paper versions
generation_runs = _course_table(
    "generation_runs",
    Column("framework_version_id", String(64), ForeignKey("framework_versions.id"), nullable=False),
    Column("catalog_version_id", String(64), ForeignKey("knowledge_catalog_versions.id"), nullable=False),
    Column("index_version_id", String(64), ForeignKey("index_versions.id"), nullable=True),
    Column("blueprint_version_id", String(64), ForeignKey("blueprint_versions.id"), nullable=False),
    Column("prompt_template_version", String(80), nullable=False),
    Column("run_type", String(60), nullable=False),
    Column("status", String(40), nullable=False, default="queued"),
    Column("contract_snapshot", JSON),
    Column("centrality_threshold_used", Float),
    Column("error_message", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
)
Index("ix_generation_runs_course_status_created", generation_runs.c.course_id, generation_runs.c.status, generation_runs.c.created_at)

generation_attempts = _course_table(
    "generation_attempts",
    Column("generation_run_id", String(64), ForeignKey("generation_runs.id"), nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("status", String(40), nullable=False, default="queued"),
    constraints=(UniqueConstraint("generation_run_id", "attempt_no", name="uq_generation_attempts_run_number"),),
)

generated_questions = _course_table(
    "generated_questions",
    Column("generation_run_id", String(64), ForeignKey("generation_runs.id"), nullable=False),
    Column("plan_item_id", String(64), ForeignKey("plan_items.id"), nullable=False),
    Column("knowledge_card_id", String(64), ForeignKey("knowledge_cards.id")),
    Column("revision_no", Integer, nullable=False, default=1),
    Column("status", String(40), nullable=False, default="candidate"),
    Column("payload", JSON, nullable=False, default=dict),
    constraints=(UniqueConstraint("generation_run_id", "plan_item_id", "revision_no", name="uq_generated_questions_run_item_revision"),),
)

quality_checks = _course_table(
    "quality_checks",
    Column("generated_question_id", String(64), ForeignKey("generated_questions.id"), nullable=False),
    Column("check_type", String(80), nullable=False),
    Column("status", String(40), nullable=False),
    Column("details", JSON, nullable=False, default=dict),
)

paper_versions = _course_table(
    "paper_versions",
    Column("exam_project_id", String(64), ForeignKey("exam_projects.id"), nullable=False),
    Column("generation_run_id", String(64), ForeignKey("generation_runs.id")),
    Column("version_no", Integer, nullable=False),
    Column("status", String(40), nullable=False, default="draft"),
    Column("created_by", String(64), ForeignKey("users.id")),
    Column("metadata", JSON, nullable=False, default=dict, server_default="{}"),
    Column("answer_detail_schema_version", String(120)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("confirmed_at", DateTime(timezone=True)),
    Column("finalized_at", DateTime(timezone=True)),
    constraints=(UniqueConstraint("exam_project_id", "version_no", name="uq_paper_versions_project_number"),),
)

paper_items = _course_table(
    "paper_items",
    Column("paper_version_id", String(64), ForeignKey("paper_versions.id"), nullable=False),
    Column("generated_question_id", String(64), ForeignKey("generated_questions.id"), nullable=False),
    Column("display_order", Integer, nullable=False),
    Column("teacher_override", JSON, nullable=False, default=dict, server_default="{}"),
    Column("finalized_text", JSON),
    Column("needs_review", Boolean, nullable=False, default=False, server_default=false()),
    Column("needs_review_reason", String(200)),
    Column("quality_audit", JSON, nullable=False, default=dict, server_default="{}"),
    constraints=(UniqueConstraint("paper_version_id", "display_order", name="uq_paper_items_version_order"),),
)

# Model observability and durable task dispatch
model_calls = _course_table(
    "model_calls",
    Column("generation_attempt_id", String(64), ForeignKey("generation_attempts.id")),
    Column("framework_build_run_id", String(64), ForeignKey("framework_build_runs.id")),
    Column("organization_run_id", String(64), ForeignKey("organization_runs.id")),
    Column("stage", String(80), nullable=False),
    Column("provider", String(80), nullable=False),
    Column("model", String(120), nullable=False),
    Column("status", String(40), nullable=False),
    Column("request_id", String(160)),
    Column("prompt_hash", String(64)),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("duration_ms", Integer),
    Column("error_code", String(120)),
    Column("error_message", Text),
    Column("details", JSON, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

task_runs = _course_table(
    "task_runs",
    Column("task_type", String(80), nullable=False),
    Column("input_version", String(160), nullable=False),
    Column("idempotency_key", String(160), nullable=False),
    Column("status", String(40), nullable=False, default="queued", server_default="queued"),
    Column("stage", String(80), nullable=False, default="queued", server_default="queued"),
    Column("progress", Integer, nullable=False, default=0, server_default="0"),
    Column("lease_owner", String(160)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("attempt", Integer, nullable=False, default=0, server_default="0"),
    Column("max_attempts", Integer, nullable=False, default=3, server_default="3"),
    Column("next_poll_at", DateTime(timezone=True)),
    Column("error_code", String(120)),
    Column("error_message", Text),
    Column("payload", JSON, nullable=False, default=dict),
    Column("result", JSON),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    constraints=(
        UniqueConstraint("course_id", "idempotency_key", name="uq_task_runs_course_idempotency"),
        CheckConstraint("status IN ('queued', 'running', 'waiting_external', 'succeeded', 'failed', 'cancelled')", name="ck_task_runs_status"),
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_task_runs_progress"),
        CheckConstraint("attempt >= 0", name="ck_task_runs_attempt"),
        CheckConstraint("max_attempts > 0", name="ck_task_runs_max_attempts"),
    ),
)
Index("ix_task_runs_course_status_type", task_runs.c.course_id, task_runs.c.status, task_runs.c.task_type)
Index("ix_task_runs_course_lease", task_runs.c.course_id, task_runs.c.status, task_runs.c.lease_expires_at)
Index("ix_task_runs_course_poll", task_runs.c.course_id, task_runs.c.status, task_runs.c.next_poll_at)

outbox_events = _course_table(
    "outbox_events",
    Column("task_run_id", String(64), ForeignKey("task_runs.id"), nullable=False),
    Column("event_type", String(80), nullable=False),
    Column("status", String(40), nullable=False, default="pending", server_default="pending"),
    Column("payload", JSON, nullable=False, default=dict),
    Column("attempts", Integer, nullable=False, default=0, server_default="0"),
    Column("available_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("claim_owner", String(160)),
    Column("claim_expires_at", DateTime(timezone=True)),
    Column("published_at", DateTime(timezone=True)),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    constraints=(
        CheckConstraint("status IN ('pending', 'claimed', 'published')", name="ck_outbox_events_status"),
        CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts"),
    ),
)
Index("ix_outbox_events_course_status_type", outbox_events.c.course_id, outbox_events.c.status, outbox_events.c.event_type)
Index("ix_outbox_events_course_available", outbox_events.c.course_id, outbox_events.c.status, outbox_events.c.available_at)
Index(
    "uq_outbox_active_task_event",
    outbox_events.c.course_id,
    outbox_events.c.task_run_id,
    outbox_events.c.event_type,
    unique=True,
    sqlite_where=outbox_events.c.status.in_(("pending", "claimed")),
    postgresql_where=outbox_events.c.status.in_(("pending", "claimed")),
)


def _same_course_fk(child_name: str, parent_column: str, parent_name: str, *, use_alter: bool = False) -> None:
    """Prevent a child in one course from pointing at another course's parent."""

    child = Base.metadata.tables[child_name]
    relationship_key = f"{child_name}:{parent_column}:{parent_name}"
    suffix = sha1(relationship_key.encode("utf-8")).hexdigest()[:10]
    child.append_constraint(
        ForeignKeyConstraint(
            [parent_column, "course_id"],
            [f"{parent_name}.id", f"{parent_name}.course_id"],
            name=f"fk_{child_name[:16]}_{parent_column[:12]}_{suffix}",
            use_alter=use_alter,
        )
    )


for _child, _column, _parent in (
    ("material_versions", "material_id", "materials"),
    ("upload_sessions", "material_id", "materials"),
    ("upload_sessions", "material_version_id", "material_versions"),
    ("document_parse_runs", "material_version_id", "material_versions"),
    ("document_parse_runs", "parser_profile_id", "parser_profiles"),
    ("document_parse_runs", "reused_from_run_id", "document_parse_runs"),
    ("document_artifacts", "document_parse_run_id", "document_parse_runs"),
    ("content_blocks", "document_parse_run_id", "document_parse_runs"),
    ("content_blocks", "material_version_id", "material_versions"),
    ("framework_versions", "framework_build_run_id", "framework_build_runs"),
    ("framework_anchors", "framework_version_id", "framework_versions"),
    ("framework_conflicts", "framework_version_id", "framework_versions"),
    ("exam_points", "framework_version_id", "framework_versions"),
    ("organization_runs", "framework_version_id", "framework_versions"),
    ("knowledge_catalog_versions", "organization_run_id", "organization_runs"),
    ("evidence_chunks", "organization_run_id", "organization_runs"),
    ("evidence_chunks", "material_version_id", "material_versions"),
    ("evidence_chunks", "content_block_id", "content_blocks"),
    ("exam_point_evidence_links", "organization_run_id", "organization_runs"),
    ("exam_point_evidence_links", "exam_point_id", "exam_points"),
    ("exam_point_evidence_links", "evidence_chunk_id", "evidence_chunks"),
    ("knowledge_catalog_versions", "framework_version_id", "framework_versions"),
    ("content_domains", "catalog_version_id", "knowledge_catalog_versions"),
    ("content_domains", "parent_domain_id", "content_domains"),
    ("assessment_units", "catalog_version_id", "knowledge_catalog_versions"),
    ("assessment_units", "content_domain_id", "content_domains"),
    ("assessment_units", "exam_point_id", "exam_points"),
    ("knowledge_cards", "catalog_version_id", "knowledge_catalog_versions"),
    ("knowledge_cards", "assessment_unit_id", "assessment_units"),
    ("knowledge_evidence_links", "knowledge_card_id", "knowledge_cards"),
    ("knowledge_evidence_links", "evidence_chunk_id", "evidence_chunks"),
    ("index_versions", "catalog_version_id", "knowledge_catalog_versions"),
    ("index_memberships", "index_version_id", "index_versions"),
    ("index_memberships", "knowledge_card_id", "knowledge_cards"),
    ("blueprint_versions", "exam_project_id", "exam_projects"),
    ("blueprint_versions", "framework_version_id", "framework_versions"),
    ("blueprint_versions", "catalog_version_id", "knowledge_catalog_versions"),
    ("blueprint_sections", "blueprint_version_id", "blueprint_versions"),
    ("blueprint_sections", "content_domain_id", "content_domains"),
    ("plan_items", "blueprint_version_id", "blueprint_versions"),
    ("plan_items", "blueprint_section_id", "blueprint_sections"),
    ("plan_items", "assessment_unit_id", "assessment_units"),
    ("plan_items", "exam_point_id", "exam_points"),
    ("plan_items", "knowledge_card_id", "knowledge_cards"),
    ("generation_runs", "framework_version_id", "framework_versions"),
    ("generation_runs", "catalog_version_id", "knowledge_catalog_versions"),
    ("generation_runs", "index_version_id", "index_versions"),
    ("generation_runs", "blueprint_version_id", "blueprint_versions"),
    ("generation_attempts", "generation_run_id", "generation_runs"),
    ("generated_questions", "generation_run_id", "generation_runs"),
    ("generated_questions", "plan_item_id", "plan_items"),
    ("generated_questions", "knowledge_card_id", "knowledge_cards"),
    ("quality_checks", "generated_question_id", "generated_questions"),
    ("paper_versions", "exam_project_id", "exam_projects"),
    ("paper_versions", "generation_run_id", "generation_runs"),
    ("paper_items", "paper_version_id", "paper_versions"),
    ("paper_items", "generated_question_id", "generated_questions"),
    ("model_calls", "generation_attempt_id", "generation_attempts"),
    ("model_calls", "framework_build_run_id", "framework_build_runs"),
    ("model_calls", "organization_run_id", "organization_runs"),
    ("outbox_events", "task_run_id", "task_runs"),
):
    _same_course_fk(_child, _column, _parent)


# exam_projects active-* references (nullable, composite cross-course FK)
# These close back-references that create a FK cycle with blueprint/generation/paper
# tables; use_alter=True lets SQLite/SA sort DROP without cycle ambiguity.
for _child, _column, _parent in (
    ("exam_projects", "active_blueprint_version_id", "blueprint_versions"),
    ("exam_projects", "active_generation_run_id", "generation_runs"),
    ("exam_projects", "active_paper_version_id", "paper_versions"),
):
    _same_course_fk(_child, _column, _parent, use_alter=True)


CORE_TABLE_NAMES = set(Base.metadata.tables)
