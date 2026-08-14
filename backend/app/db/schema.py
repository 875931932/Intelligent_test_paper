"""SQLAlchemy core schema for a fresh, empty exam-system database.

The schema deliberately contains no migration or prototype compatibility code.
All course-owned business records carry ``course_id`` so later services can
enforce tenant isolation consistently.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[str]:
    return mapped_column(String(64), primary_key=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = _id()
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="teacher")


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = (
        UniqueConstraint("owner_id", "slug", name="uq_courses_owner_slug"),
        Index("ix_courses_owner_id", "owner_id"),
    )

    id: Mapped[str] = _id()
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class Material(Base):
    __tablename__ = "materials"
    __table_args__ = (
        Index("ix_materials_course_id", "course_id"),
        UniqueConstraint("course_id", "logical_name", name="uq_materials_course_name"),
    )

    id: Mapped[str] = _id()
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    logical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    material_type: Mapped[str] = mapped_column(String(40), nullable=False, default="teaching")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="staged")


class MaterialVersion(Base):
    __tablename__ = "material_versions"
    __table_args__ = (
        UniqueConstraint("material_id", "version_no", name="uq_material_versions_number"),
        Index("ix_material_versions_course_id", "course_id"),
    )

    id: Mapped[str] = _id()
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    material_id: Mapped[str] = mapped_column(ForeignKey("materials.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="staged")
    object_key: Mapped[str | None] = mapped_column(String(500))


class FrameworkVersion(Base):
    __tablename__ = "framework_versions"
    __table_args__ = (
        Index("ix_framework_versions_course_id", "course_id"),
        UniqueConstraint("course_id", "version_no", name="uq_framework_versions_number"),
    )

    id: Mapped[str] = _id()
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="draft")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class KnowledgeCatalogVersion(Base):
    __tablename__ = "knowledge_catalog_versions"
    __table_args__ = (
        Index("ix_knowledge_catalog_versions_course_id", "course_id"),
        UniqueConstraint("course_id", "version_no", name="uq_knowledge_catalog_versions_number"),
    )

    id: Mapped[str] = _id()
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="draft")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class ContentDomain(Base):
    __tablename__ = "content_domains"
    __table_args__ = (
        Index("ix_content_domains_course_id", "course_id"),
        UniqueConstraint("course_id", "code", name="uq_content_domains_course_code"),
    )

    id: Mapped[str] = _id()
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class AssessmentUnit(Base):
    __tablename__ = "assessment_units"
    __table_args__ = (
        Index("ix_assessment_units_course_id", "course_id"),
        UniqueConstraint("course_id", "code", name="uq_assessment_units_course_code"),
    )

    id: Mapped[str] = _id()
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    weight: Mapped[int | None] = mapped_column(Integer)


class KnowledgeCard(Base):
    __tablename__ = "knowledge_cards"
    __table_args__ = (
        Index("ix_knowledge_cards_course_id", "course_id"),
        Index("ix_knowledge_cards_assessment_unit_id", "assessment_unit_id"),
    )

    id: Mapped[str] = _id()
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    assessment_unit_id: Mapped[str | None] = mapped_column(ForeignKey("assessment_units.id"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    level: Mapped[str] = mapped_column(String(10), nullable=False, default="L4")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class GenerationRun(Base):
    __tablename__ = "generation_runs"
    __table_args__ = (
        Index("ix_generation_runs_course_id", "course_id"),
        Index("ix_generation_runs_status", "status"),
    )

    id: Mapped[str] = _id()
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), nullable=False)
    run_type: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    input_version_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


CORE_TABLE_NAMES = {
    "users",
    "courses",
    "materials",
    "material_versions",
    "framework_versions",
    "knowledge_catalog_versions",
    "content_domains",
    "assessment_units",
    "knowledge_cards",
    "generation_runs",
}
